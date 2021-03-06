# -*- coding: utf-8 -*-

"""This module collects data from the national rail STOMP feeds.

The STOMPCollector class acts as the main connection handler while
feed classes deal with handling individual messages.
"""

import sys
import signal
import time
import datetime
import logging
import asyncio
import enum
import json

import stomp
import orjson

from common.config import Config
from common.mongo import Mongo


log = logging.getLogger("data_collector")


class Feeds(enum.Enum):
    """Enumeration detailing the feed implementations."""

    PPM = 0
    TD = 1
    TM = 2


class StompFeed(object):
    """Base feed handling class, which all others derive from."""

    def __init__(self, topic, durable, mongo):
        """Initialise the StompFeed.

        Args:
            name (int): "Feeds" enumeration number
            topic (str): NR topic string
            durable (str): durable connection name
            mongo (common.mongo.Mongo): database class
        """
        self.topic = topic
        self.durable = durable
        self.mongo = mongo

    async def handle_message(self, message):
        """Handle the JSON message, override in derived classes."""
        raise NotImplementedError


class PPMFeed(StompFeed):
    """Public performance metric feed handling class.

    The implementation follows the info available on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=RTPPM
    """

    def __init__(self, mongo):
        """Initialise the PPMFeed.

        Args:
            mongo (common.mongo.Mongo): database class
        """
        super().__init__("/topic/RTPPM_ALL", "thetrains-ppm", mongo)

    async def handle_message(self, message):
        """Handle the PPM JSON message."""
        try:
            parsed = orjson.loads(message)
        except orjson.JSONDecodeError:
            log.error("Can't decode STOMP message")
            return

        nat = parsed["RTPPMDataMsgV1"]["RTPPMData"]["NationalPage"]
        doc = {
            "date": datetime.datetime.fromtimestamp(
                int(parsed["RTPPMDataMsgV1"]["timestamp"]) / 1000
            ),
            "total": int(nat["NationalPPM"]["Total"]),
            "on_time": int(nat["NationalPPM"]["OnTime"]),
            "late": int(nat["NationalPPM"]["Late"]),
            "ppm": float(nat["NationalPPM"]["PPM"]["text"]),
            "rolling_ppm": float(nat["NationalPPM"]["RollingPPM"]["text"]),
        }
        log.debug("{}\n".format(doc))

        if self.mongo is not None:
            self.mongo.add("PPM", doc)


class TDFeed(StompFeed):
    """Train describer feed handling class.

    The implementation follows the info available on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=TD

    ----------------------------------------------------------------
    Whenever a `berth step' message is received we do the following...

    1) We update the `BERTHS' document for the `from' berth, removing the current train
    description and time as well as adding the `to' berth to it's `CONNECTIONS' array
    if not already there.

    2) We update the `BERTHS' document for the `to' berth, adding the new train
    description and time, as well as adding the `from' berth to it's `CONNECTIONS'
    array if not already there.

    3) We update the mongodb `TRAINS' document for that specific headcode/reporting
    number train. Appending the `to` berth and step time to their respective arrays.

    Whenever a `berth interpose' message is received we do the following...

    1) We update the `BERTHS' document for the `to' berth, adding the new train
    description and time.

    3) We update the mongodb `TRAINS' document for that specific headcode/reporting
    number train. Appending the `to` berth and time to their respective arrays.
    ----------------------------------------------------------------

    - Need to make sure the first berth in a journey is captured for each train, do we
    need to append both the `from' and `to' berths to their respective `TRAINS' arrays
    is the train has never been seen before?

    - A network graph can then be built using the BERTHS and TRAINS data, connecting
    berths together. Need to make a cut on movement time to make sure they are actually
    connected.

    - The average time taken to move between berths also gives and indication when
    averaged over multiple trains, of the distance between the two berths, the speed of
    the line and congestion. Which can be used to weight the edges of the graph etc...
    """

    def __init__(self, mongo):
        """Initialise the TDFeed.

        Args:
            mongo (common.mongo.Mongo): database class
        """
        # TODO: Get a list of TD signal area topics to follow, should be all!
        super().__init__("/topic/TD_LNW_C_SIG_AREA", "thetrains-td", mongo)

    async def handle_message(self, message):
        """Handle the TD JSON message."""
        try:
            parsed = orjson.loads(message)
        except orjson.JSONDecodeError:
            log.error("Can't decode STOMP message")
            return

        for msg in parsed:
            msg_type = list(msg.keys())[0]
            msg = msg[msg_type]
            if msg_type == "CA_MSG":  # Berth Step message
                train = msg["descr"]
                berth_from = msg["area_id"] + msg["from"]
                berth_to = msg["area_id"] + msg["to"]
                time = datetime.datetime.fromtimestamp(int(msg["time"]) / 1000)
                log.debug(
                    "TD_CA_MSG: {},{},{},{}\n".format(time, train, berth_from, berth_to)
                )

                # Generate update dictionaries
                update_from = {
                    "$set": {"LATEST_TRAIN": "0000", "LATEST_TIME": time},
                    "$addToSet": {"CONNECTIONS": berth_to},
                    "$setOnInsert": {"FIXED": False},
                }
                update_to = {
                    "$set": {"LATEST_TRAIN": train, "LATEST_TIME": time},
                    "$addToSet": {"CONNECTIONS": berth_from},
                    "$setOnInsert": {"FIXED": False},
                }
                update_train = {"$push": {"BERTHS": berth_to, "TIMES": time}}

                # Run database updates
                if self.mongo is not None:
                    self.mongo.update("BERTHS", {"NAME": berth_from}, update_from)
                    self.mongo.update("BERTHS", {"NAME": berth_to}, update_to)
                    self.mongo.update("TRAINS", {"NAME": train}, update_train)

            elif msg_type == "CC_MSG":  # Berth Interpose message
                train = msg["descr"]
                berth_to = msg["area_id"] + msg["to"]
                time = datetime.datetime.fromtimestamp(int(msg["time"]) / 1000)
                log.debug("TD_CC_MSG: {},{},{}\n".format(time, train, berth_to))

                # Generate update dictionaries
                update_to = {
                    "$set": {"LATEST_TRAIN": train, "LATEST_TIME": time},
                    "$setOnInsert": {"FIXED": False},
                }
                update_train = {"$push": {"BERTHS": berth_to, "TIMES": time}}

                # Run database updates
                if self.mongo is not None:
                    self.mongo.update("BERTHS", {"NAME": berth_to}, update_to)
                    self.mongo.update("TRAINS", {"NAME": train}, update_train)

            elif msg_type in [
                "CB_MSG",
                "CT_MSG",
                "SF_MSG",
                "SG_MSG",
                "SH_MSG",
            ]:  # Other TD messages
                pass
            else:  # should not happen
                log.warning("Received unknown TD message type: {}".format(msg_type))


class TMFeed(StompFeed):
    """Train movement feed handling class.

    The implementation follows the info available on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=Train_Movements

    We want to be able to activate and deactivate trains defined using the TD data, to
    set a `STATUS' flag that says whether that train reporting number is currently
    active. This also allows us to add extra metadata about the train in question.
    """

    def __init__(self, mongo):
        """Initialise the TMFeed.

        Args:
            mongo (common.mongo.Mongo): database class
        """
        super().__init__("/topic/TRAIN_MVT_ED_TOC", "thetrains-tm", mongo)

    async def handle_message(self, message):
        """Handle the TD JSON message."""
        try:
            parsed = orjson.loads(message)
        except orjson.JSONDecodeError:
            log.error("Can't decode STOMP message")
            return

        for msg in parsed:
            msg_type = msg["header"]["msg_type"]
            msg = msg["body"]
            if msg_type in ["0001", "0002", "0004", "0005", "0006", "0007", "0008"]:
                # [activation, cancellation, unidentified, reinstatement,
                #  change of origin, change of identity, change of location]
                pass
            elif msg_type == "0003":  # train movement
                pass
                # train_id, actual_timestamp, reporting_stanox, next_report_stanox
            else:  # should not happen
                log.warning("Received unknown TM message type")


def get_feed(feed, mongo):
    """Get the feed handling class.

    Args:
        feed: "Feeds" enumeration number
        mongo (common.mongo.Mongo): database class
    Returns:
        StompFeed: feed handler class
    """
    if feed is Feeds.PPM:
        return PPMFeed(mongo)
    elif feed is Feeds.TD:
        return TDFeed(mongo)
    elif feed is Feeds.TM:
        return TMFeed(mongo)
    else:
        log.warning("Don't recognise feed name")
        return None


class STOMPCollector(object):
    """STOMP collector class handles the connection and topic subscription.

    Uses the stomp.py package for implementing the STOMP protocol, found at
    http://jasonrbriggs.github.io/stomp.py/index.html. Follows and then builds upon
    https://wiki.openraildata.com/index.php?title=Python_Examples and implements a
    durable subscriptions outline on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=Durable_Subscription
    """

    def __init__(self, mongo, conn_attempts, nr_user, nr_pass):
        """Initialise the STOMPCollector.

        Args:
            mongo (common.mongo.Mongo): database class
            config (common.config.Config): configuration class
            conn_attempts (int): max number of connection attempts
            nr_user (str): network rail data feed username
            nr_pass (str): network rail data feed password
        """
        self.mongo = mongo  # Mongo database
        self.conn = None  # STOMP connection
        self.feeds = {}  # STOMP feed subscriptions
        self.attempts = conn_attempts
        self.nr_user = nr_user
        self.nr_pass = nr_pass

        try:  # Setup the STOMP connection to network rail feed
            self.conn = stomp.Connection(
                host_and_ports=[("datafeeds.networkrail.co.uk", 61618)],
                keepalive=True,
                vhost="datafeeds.networkrail.co.uk",
                heartbeats=(100000, 100000),
            )
            self.conn.set_listener("handler", self)  # Register self
        except Exception as e:
            log.warning("STOMP setup error ({}), continue anyway".format(e))
            self.conn = None

        # Setup signal handlers
        signal.signal(signal.SIGINT, self.exit_handler)
        signal.signal(signal.SIGTERM, self.exit_handler)

    def connect_and_subscribe(self):
        """Connect and resubscribe to all current feeds."""
        self.connect()
        for feed in self.feeds.keys():
            try:  # Attempt to subscribe to the feed
                self.conn.subscribe(
                    self.feeds[feed].topic,
                    self.feeds[feed].durable,
                    ack="client-individual",
                    headers={"activemq.subscriptionName": self.feeds[feed].durable},
                )
                log.info("Subscribed to {}".format(feed))
            except Exception as e:
                log.error("STOMP subscription error ({})".format(e))

    def connect(self):
        """Connect to the Network Rail STOMP Server."""
        for attempt in range(self.attempts):
            log.info("STOMP connection attempt: {}".format(attempt + 1))
            time.sleep(pow(attempt, 2))  # Exponential backoff in wait

            try:  # Attempt STOMP connection to Network Rail
                if self.conn is not None:
                    self.conn.connect(
                        username=self.nr_user,
                        passcode=self.nr_pass,
                        wait=True,
                        headers={"client-id": self.nr_user},
                    )
                else:
                    log.warning("No STOMP connection to connect to")
                    break

                log.info("STOMP connection successful")
                break  # Leave connection attempt loop and proceed
            except Exception as e:
                log.error("STOMP connection error ({}), retry...".format(e))

            if attempt == (self.attempts - 1):
                log.fatal("Maximum number of connection attempts made")
                sys.exit(0)

    def subscribe(self, name):
        """Subscribe to a Network Rail STOMP feed.

        Args:
            name (int): "Feeds" enumeration number
        """
        feed = get_feed(name, self.mongo)

        if name in self.feeds:  # Check if we already have this feed
            log.warning("Already subscribed to this feed")
            return

        if feed is not None:
            try:  # Attempt to subscribe to the feed
                self.conn.subscribe(
                    feed.topic,
                    feed.durable,
                    ack="client-individual",
                    headers={"activemq.subscriptionName": feed.durable},
                )
                self.feeds[name] = feed
                log.info("Subscribed to feed ({})".format(name))
            except Exception as e:
                log.error("STOMP subscription error ({})".format(e))

    def unsubscribe(self, name):
        """Unsubscribe from a Network Rail STOMP feed."""
        if name in self.feeds:
            try:
                self.conn.unsubscribe(self.feeds[name].durable)
                del self.feeds[name]
                log.info("Unsubscribed from feed ({})".format(name))
            except Exception as e:
                log.error("STOMP unsubscribe error ({}), keep feed".format(e))
                return
        else:
            log.warning("No feed with that name")

    def on_message(self, headers, message):
        """STOMP on_message handler."""
        log.debug("Got message")
        self.conn.ack(id=headers["message-id"], subscription=headers["subscription"])

        for name, feed in self.feeds.items():
            if str(feed.topic) == str(headers["destination"]):
                asyncio.run(feed.handle_message(message))
                return

    def on_error(self, headers, message):
        """STOMP on_error handler."""
        log.error("STOMP connection error '{}'".format(message))
        self.exit()

    def on_disconnected(self):
        """STOMP on_disconnected handler."""
        log.error("STOMP connection disconnect")
        self.exit()

    def on_heartbeat_timeout(self):
        """STOMP on_heartbeat_timeout handler."""
        log.error("STOMP connection heartbeat timeout")
        self.exit()

    def exit_handler(self, sig, frame):
        """Signal exit handler."""
        log.info("Exit signal handler invoked({})".format(sig))
        self.exit()

    def exit(self):
        """Exit method to close connections and exit."""
        if self.conn.is_connected():
            for feed in self.feeds.keys():
                self.conn.unsubscribe(self.feeds[feed].durable)
                log.info("Unsubscribed from feed ({})".format(feed))
            self.conn.disconnect()
            log.info("Disconnected from NR STOMP Server")
        sys.exit(0)


def main():
    """Call when data_collector starts."""
    # Setup the configuration and mongo connection
    Config.init_logging(log)
    mongo = Mongo.connect(log, Config.MONGO_URI)

    #  Populate database with known berths if not already there
    if mongo is not None and "BERTHS" not in mongo.collections():
        log.info("Loading known berths into database")
        with open("./berths.json") as berths_file:
            berths_data = json.load(berths_file)
            for key, set_data in berths_data.items():
                mongo.update("BERTHS", {"NAME": key}, {"$set": set_data})

    # Setup the STOMP national rail data feed collector and connect
    collector = STOMPCollector(
        mongo,
        Config.COLLECTOR_ATTEMPTS,
        Config.COLLECTOR_NR_USER,
        Config.COLLECTOR_NR_PASS,
    )
    collector.connect()

    # Subscribe to the configured feeds
    if Config.COLLECTOR_PPM:
        collector.subscribe(Feeds.PPM)

    if Config.COLLECTOR_TD:
        collector.subscribe(Feeds.TD)

    if Config.COLLECTOR_TM:
        collector.subscribe(Feeds.TM)

    # Infinite loop
    while 1:
        time.sleep(1)


if __name__ == "__main__":
    main()
