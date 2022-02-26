"""
PX4 to XBee or UDP software adapter script
Author: Campbell McDiarmid
"""

########################################################################################################################
#
#                                                    IMPORTS
#
########################################################################################################################


import logging
import os
import time
import subprocess
import datetime
import threading
import argparse
import json
import struct
from digi.xbee.devices import DigiMeshDevice, RemoteXBeeDevice
from digi.xbee.exception import XBeeException
from digi.xbee.util.utils import bytes_to_int
from pymavlink import mavutil
from pymavlink.dialects.v20 import ardupilotmega as mavlink
from commonlib import Fifo, MAVQueue, device_finder, replace_seq, reconnect_blocker, setup_logging
from commonlib import XBEE_PKT_SIZE, XBEE_MAX_BAUD, PX4_COMPANION_BAUD, MAV_IGNORES


########################################################################################################################
#
#                                                      CLASSES
#
########################################################################################################################


class PX4Adapter:
    """
    PX4 Adapter Class

    Currently designed for use with he following for a connection to GCS software:
    - An XBee radio if the UAV is intended to be an endpoint.  All data will be sent to a coordinator XBee radio either
      connected to a Relay UAV or GCS computer, either of which will be running gcs_adapter.py to communicate to GCS.
    - A direct UDP link to GCS software if this UAV is intended to be a Relay/Coordinator.  Does not need gcs_adapter.py
      to receive and forward messages onto GCS.
    """
    def __init__(self, settings, udp_str=None, usbcam=False):
        """
        Initializer

        :param settings: Dict of settings related to the UAV, see uav_settings.json for a template/example
        :param udp_str: String format for UDP connection target ('IP:PORT')
        """
        # Initialized lists, queues, look-up-tables and counters
        logging.info(f'PX4 adapter script initialized with udp_str={udp_str}')
        # TODO Can probably make UAVObject a parent class with a little reworking
        self.known_endpoints = []
        self.old_coordinators = []
        self.queue_out = MAVQueue()
        self.parser = mavlink.MAVLink(Fifo())
        self.settings = settings
        self.rates = settings['mav_rates']
        self.next_times = {k: time.time() + self.rates[k] for k in self.rates}
        self.seq = 0
        self.running = False
        self.px4 = None
        self.udp_str = udp_str
        self.px4_port = '/dev/ttyACM0' # device_finder('FT232R USB UART')
        self.usbcam = usbcam

    def start(self):
        """
        End blocking loop in all threads
        """
        logging.info('Starting PX4 adapter script threads')
        # Initialize threads
        _parse_thread = threading.Thread(target=self._px4_rx_thread, daemon=True)
        if self.udp_str:
            _out_thread = threading.Thread(target=self._udp_thread,  daemon=True)
        else:
            _out_thread = threading.Thread(target=self._xbee_thread, daemon=True)
        
        if self.usbcam:
            _cam_thread = threading.Thread(target=self._usb_camera_thread, daemon=True)
            _cam_thread.start()        

        # Start threads
        _parse_thread.start()
        _out_thread.start()
        self.running = True

    def stop(self):
        """
        Run all threads to completion
        """
        logging.info('Stopping PX4 adapter script threads')
        self.running = False

    def _udp_thread(self):
        """
        I. Check queue from PX4 for messages
        II. Check messages from GCS via UDP and pass onto PX4
        """
        while not self.running:
            time.sleep(0.01)

        socket = mavutil.mavudp(device=self.udp_str, input=False)

        logging.info('Started UDP message handling loop')
        while self.running:
            # I. Check queue from PX4 for messages
            while self.queue_out:
                msg_bytes = self.process_mav_message()
                socket.write(msg_bytes)

            # II. Check messages from GCS via UDP and pass onto PX4
            msg = socket.recv_msg()
            while msg:
                self.px4.write(msg.get_msgbuf())
                msg = socket.recv_msg()

            time.sleep(0.001)

        # Device closed
        socket.close()

    def _xbee_thread(self):
        """
        I.   Consume queue_out and create buffer of bytes out
        II.  Transmit bytes buffer to GCS via coordinator XBee
        III. Check for messages from coordinator, forward on to PX4 serial connection
        """
        while True:
            while not self.running:
                time.sleep(0.01)

            xbee_port = '/dev/ttyUSB0' #device_finder('XBee')
            xbee = DigiMeshDevice(xbee_port, 57600)
            xbee.open()
            coordinator = self.find_coordinator(xbee)

            logging.info('Started XBee message handling loop')
            while self.running and coordinator != None:
                # I. Consume queue_out and add to a buffer of bytes
                tx_buffer = b''
                while self.queue_out:
                    msg_bytes = self.process_mav_message()
                    tx_buffer += msg_bytes

                # II. Transmit buffered bytes, catch exceptions raised if connection has been lost
                try:
                    while tx_buffer:
                        xbee.send_data(coordinator, tx_buffer[:XBEE_PKT_SIZE])
                        tx_buffer = tx_buffer[XBEE_PKT_SIZE:]
                    message = xbee.read_data()  # Read XBee for Coordinator messages
                except XBeeException:
                    reconnect_blocker(xbee, coordinator)  # Block script until coordinator has been reconnected
                    self.queue_out.clear()  # Clear queue once reconnected
                    continue

                # III. Check whether a message was actually received, wait if no message and loop
                while message:
                    # Check who the message is from
                    if message.remote_device != coordinator:
                        # Message from another endpoint searching for a coordinator
                        logging.info('Message from another endpoint received.')
                        xbee.send_data(message.remote_device, b'ENDPT')

                    elif message.is_broadcast:
                        # If broadcast trigger handover !!! TODO
                        logging.warning('Message broadcast received - Coordinator handover initiated.')
                        self.old_coordinators.append(coordinator)
                        self.queue_out.clear()
                        self.stop()
                        time.sleep(0.01)
                        break

                    else:
                        # Message received is from coordinator, read data and pass onto PX4
                        data = message.data
                        try:
                            messages = self.parser.parse_buffer(data)
                        except mavlink.MAVError:
                            logging.exception(f'MAVError: {data}')
                        else:
                            for gcs_msg in messages:
                                msg_type = gcs_msg.get_type()

                                # Check for special message types
                                if msg_type == 'HEARTBEAT':
                                    self.heartbeat(xbee, coordinator)

                                if msg_type not in MAV_IGNORES:
                                    self.px4.write(gcs_msg.get_msgbuf())  # Write data from received message to PX4
                    last_message_received = datetime.datetime.now()
                    message = self.read_xbee_data(xbee, coordinator)

                time.sleep(0.001)

                # close this connection if we haven't heard from the base in over 15s
                if (datetime.datetime.now()- last_message_received).total_seconds()  > 15:
                    logging.warning("Closing connection to coordinator")
                    coordinator = None

            # Device closed
            xbee.close()

    def read_xbee_data(self, xbee: DigiMeshDevice, coordinator: RemoteXBeeDevice):
        """
        Error tolerant XBee reading - returns an XBee packed upon a successful read - otherwise None if an error occurs
        due to the link between xbee and coordinator radios being compromised.

        :param xbee: XBeeDevice representing XBee connected via USB to the computer running this script
        :param coordinator: RemoteXBeeDevice representing the UAV coordinator's XBee radio
        :return: XBee message from a successful read, or None if the read raised an XBeeException
        """
        try:
            message = xbee.read_data()  # Read XBee for Coordinator messages
        except XBeeException as e:
            logging.exception(e)
            reconnect_blocker(xbee, coordinator)  # Block script until coordinator has been reconnected
            self.queue_out.clear()  # Clear queue once reconnected
            message = None

        return message

    def _px4_rx_thread(self, sleep_time=0.0005):
        """
        This function serves the purpose of receiving messages from the flight controller at such a rate that no buffer
        overflow occurs.  When mav_device.recv_msg() is called, if enough data has come in from the serial connection to
        form a MAVLink packet, the packet is parsed and the latest copy of the particular type of MAVLink message is
        updated in the mav_device.messages dict.  This dict is used in the main thread for scheduling the transmission
        of each type of MAVLink packet, effectively decimating the stream to a set rate for each type of MAVLink message

        :param sleep_time: Sleep time between loops
        """
        while not self.running:
            time.sleep(0.01)

        self.px4 = mavutil.mavserial(self.px4_port, PX4_COMPANION_BAUD, source_component=1)

        while self.running:
            # Rx Message
            msg = self.px4.recv_msg()
            if not msg:  # Nothing received, wait and loop
                time.sleep(sleep_time)
                continue

            # Message received, decide what to do with it
            mav_type = msg.get_type()
            if mav_type in MAV_IGNORES:  # Ignore message
                pass
            elif mav_type not in self.rates:  # Priority Message
                self.queue_out.write(msg, priority=True)
                #logging.info(f'Priority message type: {mav_type}')
            elif time.time() >= self.next_times[mav_type]:  # Scheduled message
                self.next_times[mav_type] = time.time() + self.rates[mav_type]
                self.queue_out.write(msg, priority=False)

        self.px4.close()

    def process_mav_message(self):
        """
        Since the output rate of each MAVLink message type is decimated, the sequence byte must be replaced and CRC
        recalculated before sending the message back to the GCS.

        :return: Bytes buffer of outgoing TX data
        """
        buffer = b''
        msg = self.queue_out.read()
        msg_bytes = replace_seq(msg, self.seq)
        buffer += msg_bytes
        self.seq += 1
        return buffer

    def heartbeat(self, xbee: DigiMeshDevice, coordinator: RemoteXBeeDevice):
        """
        HEARTBEAT reply/"acknowledgement"
        Need to manually construct a RADIO_STATUS MAVLink message and place it at the front of
        priority_queue, as RADIO_STATUS messages are automatically constructed and sent back to the
        GCS on SiK radio firmware in response to a HEARTBEAT.  This is crucial for establishing a
        recognisable link on GCS software, such as QGroundControl.

        :param xbee:
        :param coordinator:
        """
        logging.debug('Generating fake heartbeat')
        rssi = bytes_to_int(xbee.get_parameter('DB'))
        remrssi = bytes_to_int(coordinator.get_parameter('DB'))
        errors = bytes_to_int(xbee.get_parameter('ER'))
        radio_status_msg = self.px4.mav.radio_status_encode(
            rssi=rssi, remrssi=remrssi, rxerrors=errors, txbuf=100, noise=0, remnoise=0, fixed=0)
        radio_status_msg.pack(self.px4.mav)
        self.queue_out.write(radio_status_msg)

    def find_coordinator(self, xbee: DigiMeshDevice):
        """
        Finds the coordinator XBee radio for targeting MAVLink data transmission

        :param xbee: Local XBee device object
        :return: Remote XBee coordinator device object
        """
        # Discover network.  Repeat until GCS has been found.
        network = xbee.get_network()
        network.add_device_discovered_callback(logging.debug)
        network.set_discovery_timeout(5)

        logging.debug('Beginning Coordinator Discovery loop.')
        while True:
            network.start_discovery_process()

            # Block until discovery is finished TODO is blocking required?
            while network.is_discovery_running():
                time.sleep(0.1)

            # Check devices on the network by Node ID
            for device in network.get_devices():
                if device not in self.known_endpoints or device not in self.old_coordinators:
                    check = self.check_coordinator(xbee, device)
                    if check is True:
                        return device

    def check_coordinator(self, xbee: DigiMeshDevice, remote: RemoteXBeeDevice):
        """
        Checks whether an unknown XBee device corresponds to a coordinator

        :param xbee: Local XBee radio
        :param remote: Remote XBee radio
        :return: None if timeout awaiting reply, True if remote is a coordinator, False if an endpoint
        """
        ret = False
        while ret != True:
            identifier = self.settings['id']
            port = self.settings['port']
            data = struct.pack('>BH', identifier, port)
            xbee.send_data(remote, data)
            rx_packet = xbee.read_data_from(remote)
            start = time.time()
            logging.info('Awaiting response or 5s timeout')

            while not rx_packet and time.time() - start < 5:
                rx_packet = xbee.read_data_from(remote)
                time.sleep(0.1)

            if not rx_packet:
                msg = f'No response: {remote}'
                ret = None
            elif rx_packet.data == b'COORD':
                msg = f'Coordinator found: {remote}'
                self.known_endpoints.append(remote)
                ret = True
            elif rx_packet.data == b'ENDPT':
                msg = f'Endpoint found: {remote}'
                ret = False
            else:
                except_msg = f'Unexpected data packet {rx_packet.data}'
                logging.exception(except_msg)
                ret = False

        logging.info(msg)
        return ret

    def _usb_camera_thread(self):
        """
        Records video from a USB camera
        TODO Add arguments for resolution, format, source, fps, or entire ffmpeg call.
        """

        while not self.running or self.px4 is None:
            time.sleep(0.01)

        call = "ffmpeg -f v4l2 -framerate 60 -video_size 1960x1080 -input_format mjpeg -i /dev/video0"
        env = os.environ.copy()
        video_subproccess = subprocess.Popen("/bin/bash", stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, shell=True, env=env)

        # Loop forever
        while self.running:
            # Trigger on Take-Off (Armed)
            logging.info('Waiting for vehicle to arm.')
            try:
                while not self.px4.motors_armed():
                    time.sleep(1)
            except Exception as e:
                logging.exception(e)
                break
            
            # Start video code
            logging.info('Vehicle armed.')

            timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
            filename = f"videos/{timestamp}.mkv"
            video_subproccess.stdin.write(f"{call} {filename}\n".encode())
            video_subproccess.stdin.flush()
            
            logging.info('Video recording started.')

            # Trigger on Landing (Disarmed)
            try:
                while self.px4.motors_armed():
                    time.sleep(1)
            except Exception as e:
                logging.exception(e)
            
            # End recording code
            logging.info('Vehicle disarmed.')
            video_subproccess.stdin.write(b"q")
            video_subproccess.stdin.flush()
            logging.info('Recording stopped.')
        
        video_subprocess.kill()
        logging.info('Video subprocess killed.')

########################################################################################################################
#
#                                                      CLASSES
#
########################################################################################################################


def main(settings, udp_str=None, usbcam=False):
    """
    Infinite loop that establishes a connection to GCS either directly over UDP or via a coordinator XBee radio
    depending whether or not udp_str is specified.

    :param settings: Dict of settings/parameters related to the UAV running this software
    :param udp_str: String representation of UDP link ('IP:PORT')
    """
    px4_adapter = PX4Adapter(settings, udp_str, usbcam)
    while True:
        px4_adapter.start()
        while px4_adapter.running:
            time.sleep(0.1)

        time.sleep(1)


if __name__ == '__main__':
    # Argument passing for PX4 adapter script
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--settings', type=str, required=False, default='uav_settings.json',
        help='Path to UAV settings json file.')
    parser.add_argument(
        '--ssh', action='store_true',
        help='SSH_CONNECTION environment variable when using SSH from GCS computer over WiFi (relay only).')
    parser.add_argument(
        '--usbcam', action='store_true',
        help='Store video recordings while armed if a USB Camera is connected.')
    args = parser.parse_args()

    # JSON File
    _json_file = open(args.settings, 'r')
    _uav_settings = json.load(_json_file)
    _port = _uav_settings['port']

    # UDP link directly between GCS and UAV (WiFi only)
    if args.ssh:
        _ip, *_ = os.environ['SSH_CONNECTION'].split(' ')
        _udp = f'{_ip}:{_port}'
    else:
        _udp = None
    setup_logging()
    main(_uav_settings, udp_str=_udp, usbcam=args.usbcam)
