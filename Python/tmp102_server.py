import logging
from argparse import ArgumentParser
from http.server import SimpleHTTPRequestHandler, HTTPServer
from http import HTTPStatus
import quick2wire.i2c as i2c
from quick2wire.selector import Selector, Semaphore, Timer
import Adafruit_LCDPlate
from quick2wire.parts.mcp23x17 import INTCAPA

from logging.handlers import RotatingFileHandler
from threading import Thread
from time import ctime, time, sleep
from base64 import b64encode, b64decode
import threading
import sys
import signal

#import base64

TMP100A_ADDRESS = 0x48
TMP100B_ADDRESS = 0x4a

TEMP_REGISTER = 0x00 # RO
CONFIG_REGISTER = 0x01 # R/W
TLOW_REGISTER = 0x02 # R/W
THIGH_REGISTER = 0x03 # R/W

"""
 9 bit, resolution: 0.5 C
10 bit, resolution: 0.25 C
11 bit, resolution: 0.125 C
12 bit, resolution: 0.0625 C
"""
BITS = 10
LOG_INTERVAL = 1800
LCD_INTERVAL = 30

TEMPERATURE_CSV_FILE = "temperatures.csv"

HTTP_SERVER_PORT = 8001
LOCAL_ADDRESS_PREFIX = "192.168.1." # client addresses starting with this do not need to authenticate

TEMPERATURES_HTML = """\
<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN"
        "http://www.w3.org/TR/html4/strict.dtd">
<html>
    <head>
        <meta http-equiv="Content-Type" content="text/html;charset=utf-8">
        <title>Current Temperature</title>
        <style>body {{font-family:sans-serif}}</style>
    </head>
    <body>
        <h1>Temperature at {0}</h1>
        <h2><table border="1" cellspacing="0" cellpadding="5">
        <tr align="right"><td>Inside</td><td>{1}C</td><td>{2:.2f}F</td></tr>
        <tr align="right"><td>Outside</td><td>{3}C</td><td>{4:.2f}F</td></tr>
        </table></h2>
        <a href="temperatures.csv">temperatures.csv</a>&nbsp;&nbsp;
        <a href="/rotateLog">rotate</a>
    </body>
</html>
"""

def c2f(c):
    return 1.8 * c + 32

FMT_ALL = "{0:6}C, {1:.2f}F\n{2:6}C, {3:.2f}F"
FMT_C = "Inside:  {0}C\nOutside: {2}C"
FMT_F = "Inside:  {1:.2f}F\nOutside: {3:.2f}F"
FMT_INSIDE = "Inside:\n{0:6}C, {1:.2f}F"
FMT_OUTSIDE = "Outside:\n{2:6}C, {3:.2f}F"

class TempLogRecord(object):
    def __init__(self, timestamp, temperatures):
        self.timestamp = timestamp
        self.temperatures = temperatures
    def getMessage(self):
        return ctime(self.timestamp) + "," +\
               ",".join([",".join([str(t), str(c2f(t))]) for t in self.temperatures])

class TempFormatter(object):
    def format(self, record):
        return record.getMessage()

class TemperatureLogger():
    """Starts recording temperatures from two TMP100 chips attached to the I2C bus.
    Resolution determined by BITS.
    Sensors read every LOG_INTERVAL, recording to TEMPERATURE_CSV_FILE.
    """
    semaphore = Semaphore(blocking=False)
    log_timer = Timer(offset=1, interval=LOG_INTERVAL)

    def __init__(self):
        self._terminated = False
        self.timestamp = None
        self.temperatures = [None, None]
        self.log_handler = RotatingFileHandler(TEMPERATURE_CSV_FILE, backupCount=10)
        self.log_handler.setFormatter(TempFormatter())

    def run(self):
        logging.info("Temperature logging starting...")

        with i2c.I2CMaster() as bus,\
             Selector() as selector,\
             self.semaphore,\
             self.log_timer,\
             Timer(offset=0.1, interval=LCD_INTERVAL) as lcd_timer,\
             Adafruit_LCDPlate.get_interrupt_pin() as int_pin:

            # tolerate missing LCD
            lcd = None
            try:
                lcd = Adafruit_LCDPlate.Adafruit_LCDPlate(bus)
                fmt = FMT_ALL
                logging.info("LCD is attached")
            except IOError:
                logging.info("LCD is not attached")

            # resolution (9-12 bit) gets stored as (0-3) in bits 5,6 of config reg
            config = (BITS - 9) << 5
            bus.transaction(
                i2c.writing_bytes(TMP100A_ADDRESS, CONFIG_REGISTER, config),
                i2c.writing_bytes(TMP100B_ADDRESS, CONFIG_REGISTER, config))

            for addr in (TMP100A_ADDRESS, TMP100B_ADDRESS):
                read_results = bus.transaction(
                    i2c.writing_bytes(addr, CONFIG_REGISTER),
                    i2c.reading(addr, 1))
                # should be 0x80 at startup/reset
                logging.info("conf register for 0x{0:02x}: 0x{1:02x}".format(addr, read_results[0][0]))

            right_shift = 16 - BITS
            div = 1 << (BITS - 8)

            selector.add(self.semaphore)
            selector.add(self.log_timer)
            self.log_timer.start()

            if lcd:
                selector.add(lcd_timer)
                selector.add(int_pin)
                lcd_timer.start()

            while not self._terminated:
                try:
                    if lcd:
                        lcd.set_led(Adafruit_LCDPlate.GREEN_PIN, False)

                    selector.wait()
                    if self._terminated:
                        break
                    if not selector.ready is int_pin:
                        selector.ready.wait() # consume event

                    for i, addr in enumerate((TMP100A_ADDRESS, TMP100B_ADDRESS)):
                        read_results = bus.transaction(
                            i2c.writing_bytes(addr, TEMP_REGISTER),
                            i2c.reading(addr, 2))
                        t = read_results[0]
                        self.temperatures[i] = (t[0] if t[0] <= 127 else t[0] - 256) + (t[1] >> right_shift) / div

                    self.timestamp = time()

                    if lcd:
                        lcd.set_led(Adafruit_LCDPlate.GREEN_PIN, True)
                        lcd.set_led(Adafruit_LCDPlate.RED_PIN, False)

                    if selector.ready is self.log_timer:
                        self.log_handler.emit(TempLogRecord(self.timestamp, self.temperatures))
                    elif selector.ready is lcd_timer:
                        lcd.clear()
                        lcd.message(fmt.format(
                            self.temperatures[0], c2f(self.temperatures[0]),
                            self.temperatures[1], c2f(self.temperatures[1])))
                    elif selector.ready is int_pin:
                        # this is hacky...
                        intcap = lcd.chip.registers.read_register(INTCAPA) # required?
                        button_state = lcd.get_button_state()
                        if button_state == 0:
                            pass # button up event: ignore
                        else:
                            if lcd.button_state_pressed(button_state, Adafruit_LCDPlate.SELECT):
                                fmt = FMT_ALL
                            elif lcd.button_state_pressed(button_state, Adafruit_LCDPlate.UP):
                                fmt = FMT_INSIDE
                            elif lcd.button_state_pressed(button_state, Adafruit_LCDPlate.DOWN):
                                fmt = FMT_OUTSIDE
                            elif lcd.button_state_pressed(button_state, Adafruit_LCDPlate.LEFT):
                                fmt = FMT_C
                            elif lcd.button_state_pressed(button_state, Adafruit_LCDPlate.RIGHT):
                                fmt = FMT_F
                            else:
                                fmt = "Unknown\nButton!"
                            lcd_timer.stop()
                            lcd_timer.start()
                except KeyboardInterrupt:
                    logging.info("Keyboard interrupt received")
                    self._terminated = True
                except SystemExit:
                    logging.info("SystemExit received")
                    self._terminated = True
                except:
                    logging.exception("Error reading sensors:")
                    if lcd:
                        lcd.set_led(Adafruit_LCDPlate.RED_PIN, True)
            if lcd:
                lcd.clear()
                lcd.set_backlight(False)
                lcd.set_led(Adafruit_LCDPlate.GREEN_PIN, False)
                lcd.set_led(Adafruit_LCDPlate.RED_PIN, False)

        logging.info("Temperature logging stopped.")
        self.log_handler.close()

    def terminate(self):
        self._terminated = True
        self.semaphore.signal()

    def rotate_log(self):
        self.log_timer.stop()
        self.log_handler.doRollover()
        self.log_timer.start()

class Cipher(object):
    """ A simple way to encrypt and decrypt passwords that are stored in the config file. """
    def __init__(self, key):
        self.key = key
    def encrypt(self, pw):
        return b64encode(bytes([(x^y) for x,y in zip(pw.encode(), self.key)])).decode()
    def decrypt(self, pw):
        return bytes([(x^y) for x,y in zip(b64decode(pw.encode()), self.key)]).decode()

class MyHTTPRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1" # keeps connections alive by default
    timeout = 15 # request socket timeout

    unrestricted = {"/robots.txt", "/favicon.ico"}

    def do_GET(self):
        is_local = self.client_address[0].startswith(LOCAL_ADDRESS_PREFIX)
        if not is_local and not self.path in self.unrestricted:
            if self.headers.get('Authorization') == None:
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header('WWW-Authenticate', 'Basic realm=\"TempsRealm\"')
                self.send_header("Content-Length", 0)
                self.end_headers()
                return
            authorization = self.headers.get('Authorization').split()
            #auth_type = authorization[0]
            #print(authorization[0], authorization[1])
            #b64encode("username:passwd".encode())
            if len(authorization) != 2 or \
               authorization[0] != "Basic" or \
               authorization[1] != self.auth:
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", 0)
                self.end_headers()
                return
        if self.path == "/rotateLog":
            self.log_message("Rotating %s", TEMPERATURE_CSV_FILE)
            self.server.temperature_logger.rotate_log()
            self.send_content(b"OK", "text/plain")
        elif self.path == "/temps":
            tl = self.server.temperature_logger
            tl.semaphore.signal()
            sleep(0.5)
            self.send_content(TEMPERATURES_HTML.format(
                ctime(tl.timestamp),
                tl.temperatures[0], c2f(tl.temperatures[0]),
                tl.temperatures[1], c2f(tl.temperatures[1])).encode("UTF-8", "replace"),
                "text/html")
        elif self.path == "/shutdown":
            self.log_message("Shutting down...")
            self.close_connection = True
            # terminate the temperature_logger thread
            self.server.temperature_logger.terminate()
            self.send_content(b"OK", "text/plain")

        else:
            if self.server.logfilename and self.path == "/" + self.server.logfilename:
                logging.getLogger().handlers[0].flush()
            elif self.path == "/" + TEMPERATURE_CSV_FILE:
                self.server.temperature_logger.log_handler.flush()
            super().do_GET()

    def send_content(self, content, mimeType):
        self.send_response(HTTPStatus.OK)
        self.send_header("Cache-Control", "max-age=0,no-cache") # dynamic content
        self.send_header("Content-Type", mimeType)
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, msg, *args):
        """ Override to log an arbitrary message.
            Note: msg uses old printf-style formatting """
        logging.info("[HTTP] %s - %s",
            self.address_string(),
            msg%args)

    SimpleHTTPRequestHandler.extensions_map.update({
        '.csv': 'text/plain',
        '.log': 'text/plain',
        })

def sighandler(signum, frame):
    logging.debug("Signal handler called with signal %d", signum)
    #if signum == signal.SIGHUP:
    #    # reload properties
    #    pass
    if signum == signal.SIGTERM:
        sys.exit(0)
signal.signal(signal.SIGTERM, sighandler)
#signal.signal(signal.SIGHUP, sighandler)

def main():
    parser = ArgumentParser(description="Temperature Logger")
    #parser.add_argument("--testing", "-t", action="store_true")
    parser.add_argument("--keyfile", "-k")
    parser.add_argument("--encrypt", "-e")
    parser.add_argument("--logfile", "-l")
    parser.add_argument("--loglevel", "-d", default=logging.DEBUG)
    args = parser.parse_args()

    if args.logfile:
        class MyLogger(object):
            """Redirect stdio to log file."""
            def __init__(self, level):
                self.level = level

            def write(self, message):
                # Only log if there is a message (not just a new line)
                if message.rstrip() != "":
                    logging.log(self.level, message.rstrip())

            def flush(self):
                pass

        # Replace stdout with logging to file at INFO level
        sys.stdout = MyLogger(logging.INFO)
        # Replace stderr with logging to file at ERROR level
        sys.stderr = MyLogger(logging.ERROR)

    logging.basicConfig(
        format="%(asctime)s: %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        filename=args.logfile,
        filemode="w", # start afresh
        level=args.loglevel)

    #if args.testing:
    #    ...

    # key for {en|de}crypting passwords
    if args.keyfile:
        try:
            with open(args.keyfile, "rb") as f:
                key = f.read()
        except:
            logging.error("Cannot open {en|de}cryption key: %s", args.keyfile)
            sys.exit(3)
    else:
        try:
            key = os.environ["PYCRYPTKEY"].encode()
        except:
            logging.error("Missing {en|de}cryption key environment variable PYCRYPTKEY")
            sys.exit(3)

    if args.encrypt:
        # encrypt and print out given password, then exit
        print(Cipher(key).encrypt(args.encrypt))
        sys.exit(0)

    MyHTTPRequestHandler.auth =
        b64encode(Cipher(key).decrypt("Ei8GDSEJOhI=").encode()).decode()

    temperature_logger = TemperatureLogger()

    with HTTPServer(("", HTTP_SERVER_PORT), MyHTTPRequestHandler) as http_server:
        # Allow server (and handler) access to TemperatureLogger
        http_server.temperature_logger = temperature_logger
        http_server.logfilename = args.logfile
        sa = http_server.socket.getsockname()
        logging.info("Serving HTTP on {} port {}...".format(sa[0], sa[1]))
        Thread(
            name = "HTTP Server",
            target = http_server.serve_forever,
            kwargs = {"poll_interval" : 5},
            daemon=False).start()
        try:
            temperature_logger.run()
        finally:
            logging.info("Shutting down HTTP Server...")
            http_server.shutdown() # breaks the serve_forever() loop
    logging.info("Done.")
    logging.shutdown()

if __name__ == "__main__":
    main()
