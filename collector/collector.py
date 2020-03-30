# -*- coding: utf-8 -*-

import os
import sys
import signal
from time import sleep
from datetime import datetime
import json

from sqlalchemy import create_engine  
from sqlalchemy import Column, Integer, Float  
from sqlalchemy.ext.declarative import declarative_base  
from sqlalchemy.orm import sessionmaker
import stomp  

base = declarative_base()


class PPM(base):
    __tablename__ = 'ppm'

    date = Column(Integer, primary_key=True)
    total = Column(Integer)
    on_time = Column(Integer)
    late = Column(Integer)
    ppm = Column(Float)
    rolling_ppm = Column(Float)


class Collector(object):
    def __init__(self, session):
        self.session = session
        self.counter = 0
        self.init()

    def init(self):
        # TODO: Use the heartbeat protocol
        self.conn = stomp.Connection(
            host_and_ports=[('datafeeds.networkrail.co.uk', 61618)],
            keepalive=True,
            vhost='datafeeds.networkrail.co.uk'
        )

        self.conn.set_listener('', self)
        signal.signal(signal.SIGINT, self.exit_handler)
        signal.signal(signal.SIGTERM, self.exit_handler)

    def connect_and_subscribe(self):
        # We use an exponential backoff and a max number of retry limit
        print("Attempting connection...")
        if self.counter >= 10:
            print('Maximum number of connection attempts made!')
            sys.exit(0)
        for wait in range(self.counter):
            sleep(pow(wait, 2))

        self.counter += 1
        self.conn.connect(
            username=os.environ["NETWORK_RAIL_USER"],
            passcode=os.environ["NETWORK_RAIL_PASS"],
            wait=True,
            headers={'client-id': os.environ["NETWORK_RAIL_USER"]}
        )
        self.conn.subscribe(
            '/topic/RTPPM_ALL',
            'thetrains-rtppm',
            ack='client-individual',
            headers={'activemq.subscriptionName': 'thetrains-rtppm'}
        )
        self.counter = 0  # Reset counter to zero
        print("connected!")

    def on_message(self, headers, message):
        self.conn.ack(
            id=headers['message-id'],
            subscription=headers['subscription']
        )

        # Parse the message JSON and extract the required fields
        parsed = json.loads(message)
        timestamp = int(parsed['RTPPMDataMsgV1']['timestamp']) / 1000
        parsed = parsed['RTPPMDataMsgV1']['RTPPMData']['NationalPage']
        total = int(parsed['NationalPPM']['Total'])
        on_time = int(parsed['NationalPPM']['OnTime'])
        late = int(parsed['NationalPPM']['Late'])
        ppm = float(parsed['NationalPPM']['PPM']['text'])
        rolling_ppm = float(parsed['NationalPPM']['RollingPPM']['text'])

        date = datetime.fromtimestamp(timestamp)
        date = date.strftime('%Y-%m-%d %H:%M:%S')

        print('{}: ({},{},{}), ({},{})'.format(
            date, total, on_time, late, ppm, rolling_ppm
        ))

        ppm_record = PPM(
            date=timestamp,
            total=total,
            on_time=on_time,
            late=late,
            ppm=ppm,
            rolling_ppm=rolling_ppm)

        self.session.add(ppm_record)
        self.session.commit()

    def on_error(self, headers, message):
        print('received an error "{}"'.format(message))
        self.connect_and_subscribe(self.conn)

    def on_disconnected(self):
        print('disconnected')
        self.connect_and_subscribe(self.conn)

    def exit_handler(self, sig, frame):
        print('Disconnecting...')
        self.conn.disconnect()
        self.session.close()
        sys.exit(0)


def main():
    try:
        db = create_engine(os.environ["DATABASE_URL"])
        print(db.table_names())
    except Exception:
        print("Can't connect to database!")
        sys.exit(0)

    Session = sessionmaker(db)
    session = Session()
    base.metadata.create_all(db)

    collector = Collector(session)
    collector.connect_and_subscribe()

    while 1:
        sleep(1)


if __name__ == '__main__':
    main()
