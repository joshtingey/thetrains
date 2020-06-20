# -*- coding: utf-8 -*-

"""
This module collects data from the national rail STOMP feeds. The
STOMPCollector class acts as the main connection handler while Feed
classes deal with handling individual messages.
"""

import sys
import signal
import time
import datetime
import logging
import asyncio
import enum

from decouple import config
import stomp
import orjson

from common.config import config_dict
from common.mongo import Mongo


log = logging.getLogger("collector")


class Feeds(enum.Enum):
    """Enumeration detailing the feed implementations.
    """

    PPM = 0
    TD = 1
    TM = 2


class StompFeed(object):
    """Base feed handling class, which all others derive from.
    """

    def __init__(self, topic, durable, mongo):
        """Initialise the StompFeed.
        Args:
            name (int): "Feeds" enumeration number
            topic (str): NR topic string
            durable (str): Durable connection name
            mongo (common.mongo.Mongo): Database class
        """
        self.topic = topic
        self.durable = durable
        self.mongo = mongo

    async def handle_message(self, message):
        """Handle the JSON message, override in derived classes
        """
        raise NotImplementedError


class PPMFeed(StompFeed):
    """Public performance metric feed handling class.
    The implementation follows the info available on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=RTPPM
    """

    def __init__(self, mongo):
        """Initialise the PPMFeed.
        Args:
            mongo (common.mongo.Mongo): Database class
        """
        super().__init__("/topic/RTPPM_ALL", "thetrains-ppm", mongo)

    async def handle_message(self, message):
        """Handle the PPM JSON message.
        """
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
            self.mongo.add("ppm", doc)


class TDFeed(StompFeed):
    """Train describer feed handling class.
    The implementation follows the info available on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=TD

    Whenever a 'berth step' message is received, we 'update' a mongodb document
    for the train it describes. The new berth is appended to a list of berths the
    train has passed through as well as the time this happened.

    Once a train has reached it destination, we set a 'complete' flag on the document.
    As multiple trains with the same headcode/reporting number are possible within
    a single day, if no activity is seen on a train for a hour we also set it as 'complete'

    If the train has never been seen before, we append both the from and to berths
    to the document.

    This then allows us to build up a network graph of the country using the list of berths
    for each train, showing which berths are connected. As some berths in different train
    describer areas can actually represent the sme physical train location we make a cut on
    the time between berths and cluster any that occur within n seconds as being the same
    physical location.

    The time taken to move between berths also gives and indication when averaged over multiple
    trains, of the distance between the two berths, the speed of the line and congestion.
    """

    def __init__(self, mongo):
        """Initialise the TDFeed.
        Args:
            mongo (common.mongo.Mongo): Database class
        """
        # TODO: Get a list of TD signal area topics to follow, should be all!
        super().__init__("/topic/TD_LNW_C_SIG_AREA", "thetrains-td", mongo)

    async def handle_message(self, message):
        """Handle the TD JSON message.
        """
        try:
            parsed = orjson.loads(message)
        except orjson.JSONDecodeError:
            log.error("Can't decode STOMP message")
            return

        for msg in parsed:
            msg_type = list(msg.keys())[0]
            msg = msg[msg_type]
            if msg_type == "CA_MSG":  # berth step
                descr = msg["descr"]
                berth = str(msg["area_id"] + msg["to"])
                time = datetime.datetime.fromtimestamp(int(msg["time"]) / 1000)
                log.debug("{},{},{}\n".format(descr, berth, time))
                self.mongo.client["td"].update_one(
                    {"descr": descr},
                    {"$push": {"berth": berth, "time": time}},
                    upsert=True,
                )
            elif msg_type == "CB_MSG":  # berth cancel
                pass
            elif msg_type == "CC_MSG":  # berth interpose
                pass
            elif msg_type == "CT_MSG":  # heartbeat
                pass
            elif msg_type == "SF_MSG":  # signalling update
                pass
            elif msg_type == "SG_MSG":  # signalling refresh
                pass
            elif msg_type == "SH_MSG":  # signalling refresh finished
                pass
            else:  # should not happen
                log.warning("Received unknown TD message type")


class TMFeed(StompFeed):
    """Train movement feed handling class.
    The implementation follows the info available on the openraildata wiki at,
    https://wiki.openraildata.com/index.php?title=Train_Movements
    """

    def __init__(self, mongo):
        """Initialise the TMFeed.
        Args:
            mongo (common.mongo.Mongo): Database class
        """
        super().__init__("/topic/TRAIN_MVT_ED_TOC", "thetrains-tm", mongo)

    async def handle_message(self, message):
        """Handle the TD JSON message.
        """
        try:
            parsed = orjson.loads(message)
        except orjson.JSONDecodeError:
            log.error("Can't decode STOMP message")
            return

        """
        - we start tracking a train when we see a train movement message
        - we append stanox locations and times to lists as we get move movement messages
        - we also append TD+berth codes to a list as we see movements in the train
          describer messages by matching the train headcode to its code here.
        - we also record the latest timestamp of any event happening for that train
        - once a train has not had anything happen to it for an hour we close it

        - we can then build a graph of berth->berth connections

        Assumptions:
            - No two trains can ever have the same id on the same day
        """

        for msg in parsed:
            msg_type = msg["header"]["msg_type"]
            msg = msg["body"]
            if msg_type in ["0001", "0002", "0004", "0005", "0006", "0007", "0008"]:
                # [activation, cancellation, unidentified, reinstatement,
                #  change of origin, change of identity, change of location]
                pass
            elif msg_type == "0003":
                pass
                # train movement
                # train_id, actual_timestamp, reporting_stanox, next_report_stanox
            else:
                # should not happen
                log.warning("Received unknown TM message type")


def get_feed(feed, mongo):
    """Get the feed handling class.
    Args:
        feed: "Feeds" enumeration number
        mongo (common.mongo.Mongo): Database class
    Returns:
        StompFeed: Feed handler class
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

    def __init__(self, mongo, config):
        """Initialise the STOMPCollector.
        Args:
            mongo (common.mongo.Mongo): Database class
            config (common.config.Config): Configuration class
        """
        self.mongo = mongo  # Mongo database
        self.conn = None  # STOMP connection
        self.feeds = {}  # STOMP feed subscriptions
        self.attempts = config.CONN_ATTEMPTS  # Max number of conn attempts
        self.nr_user = config.NR_USER  # Network rail username
        self.nr_pass = config.NR_PASS  # Network rail password

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
        """Connect and resubscribe to all current feeds.
        """
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
        """Connect to the Network Rail STOMP Server.
        """
        for attempt in range(self.attempts):
            log.info("STOMP connection attempt: {}".format(attempt + 1))
            time.sleep(pow(attempt, 2))  # Exponential backoff in wait

            try:  # Attempt STOMP connection to Network Rail
                log.info("Using {}:{}".format(self.nr_user, self.nr_pass))
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
        """Unsubscribe from a Network Rail STOMP feed.
        """
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
        """STOMP on_message handler.
        """
        log.debug("Got message")
        self.conn.ack(id=headers["message-id"], subscription=headers["subscription"])

        for name, feed in self.feeds.items():
            if str(feed.topic) == str(headers["destination"]):
                asyncio.run(feed.handle_message(message))
                return

    def on_error(self, headers, message):
        """STOMP on_error handler.
        """
        log.error("STOMP connection error '{}'".format(message))
        self.exit()

    def on_disconnected(self):
        """STOMP on_disconnected handler.
        """
        log.error("STOMP connection disconnect")
        self.exit()

    def on_heartbeat_timeout(self):
        """STOMP on_heartbeat_timeout handler.
        """
        log.error("STOMP connection heartbeat timeout")
        self.exit()

    def exit_handler(self, sig, frame):
        """Signal exit handler to close connections and exit.
        """
        log.info("Exit signal handler invoked({})".format(sig))
        self.exit()

    def exit(self):
        if self.conn.is_connected():
            for feed in self.feeds.keys():
                self.conn.unsubscribe(self.feeds[feed].durable)
                log.info("Unsubscribed from feed ({})".format(feed))
            self.conn.disconnect()
            log.info("Disconnected from NR STOMP Server")
        sys.exit(0)


def main():
    """Main function called when collector starts.
    """
    conf = config_dict[config("ENV", cast=str, default="local")]
    conf.init_logging(log)

    mongo = Mongo(log, conf.MG_URI)

    collector = STOMPCollector(mongo, conf)
    collector.connect()

    if conf.PPM_FEED:
        collector.subscribe(Feeds.PPM)

    if conf.TD_FEED:
        collector.subscribe(Feeds.TD)

    if conf.TM_FEED:
        collector.subscribe(Feeds.TM)

    while 1:
        time.sleep(1)


if __name__ == "__main__":
    main()
