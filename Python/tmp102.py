import quick2wire.i2c as i2c
from quick2wire.gpio import pi_header_1, In, PullUp, Both
from quick2wire.selector import Selector

TEMPERATURE_REG = 0x00 # RO
CONFIG_REG = 0x01 # R/W
T_LOW_REG = 0x02 # R/W
T_HIGH_REG = 0x03 # R/W

ADDRESSES = [0x48, 0x49, 0x4A, 0x4B]

ALERT_PIN = 7

def c2f(c):
    return 1.8 * c + 32

"""
Config bits for TMP102 are:
    Byte 1:
        OS,R1,R0,F1,F0,POL,TM,SD
    Byte 2:
        CR1,CR0,AL,EM,0,0,0,0

    EM  - Extended Mode
          0=Normal mode, 12 bit resolution, compatible with TMP75
          1=Extended mode, 13 bit resolution
    AL  - Alert bit (R/O)
    CR  - Conversion rate: 00=0.25Hz, 01=1Hz, 10=4Hz, 11=8Hz
    SD  - Shutdown Mode: 1=shutdown until request to read, 0=Continuous
    TM  - Thermostat Mode: 0=Comparator mode (like a thermostat), 1=Interrupt mode
    POL - Polarity of Alert pin
    F   - Fault Queue: 00=1, 01=2, 10=4, 11=6 consecutive faults
    R   - Converter resolution (R/O) = 11 (12 bit) for compatibility
    OS  - One Shot mode: when SD=1, OS=1 does a read and then shutdown
"""

class TMP102():
    def __init__(self, bus, address=ADDRESSES[0]):
        if address not in ADDRESSES:
            raise ValueError("Invalid Address: {0:#x}".format(address))
        self.bus = bus
        self.address = address

        res = bus.transaction(
            i2c.writing_bytes(address, CONFIG_REG),
            i2c.reading(address, 2))
        r = res[0]
        print("Initial conf registers: 0x{:02x}, 0x{:02x}".format(r[0], r[1]))
        self._ext = (r[1] >> 4) & 0x01
        print("extended_mode", self._ext)

    def _bytesToTemp(self, data):
        # Adjustment for extended mode
        #ext = self._extractConfig(1, 4, 1)
        #ext = data[1] & 0x01
        res = int((data[0] << (4+self._ext)) + (data[1] >> (4-self._ext)))

        if (data[0] & 0x80) is 0x80:
            # Perform 2's complement operation (x = x - 2^bits)
            res = res - (1 << (12+self._ext))
        return res * 0.0625

    def _tempToBytes(self, temp):
        res = int(temp/0.0625)
        #ext = self._extractConfig(1, 4, 1)
        if res < 0:
            res = res + (1 << (12+self._ext))
        hi = res >> (4+self._ext)
        lo = ((res << (4-self._ext)) & 0xFF) | self._ext
        return [hi, lo]

    def _extractConfig(self, num, location=0, length=8):
        res = self.bus.transaction(
            i2c.writing_bytes(self.address, CONFIG_REG),
            i2c.reading(self.address, 2))
        data = res[0]
        mask = (1 << length) - 1
        return (data[num] >> location) & mask

    def _injectConfig(self, num, location, length, setting):
        mask = ((1 << length) - 1) << location
        setting = (setting << location) & mask
        res = self.bus.transaction(
            i2c.writing_bytes(self.address, CONFIG_REG),
            i2c.reading(self.address, 2))
        data = list(*res)
        data[num] &= ~mask
        data[num] |= setting
        self.bus.transaction(
            i2c.writing_bytes(self.address, CONFIG_REG, data[0], data[1]))

    def readTemperature(self):
        res = self.bus.transaction(
            i2c.writing_bytes(self.address, TEMPERATURE_REG),
            i2c.reading(self.address, 2))
        return self._bytesToTemp(res[0])

    def setConversionRate(self, rate):
        # 0 : 0.25 Hz
        # 1 : 1 Hz
        # 2 : 4 Hz (default)
        # 3 : 8 Hz
        self._injectConfig(1, 6, 2, rate)

    def setExtendedMode(self, mode):
        # 0 : 12-bit ( -55C to 128C)
        # 1 : 13-bit ( -55C to 150C)
        self._injectConfig(1, 4, 1, mode)
        self._ext = mode

    def sleep(self):
        self._injectConfig(0, 0, 1, True)

    def wakeup(self):
        self._injectConfig(0, 0, 1, False)

    def setAlertPolarity(self, polarity):
        # 0 : Active Low
        # 1 : Active High
        self._injectConfig(0, 2, 1, polarity)

    def alert(self):
        return self._extractConfig(1, 5, 1)

    def setFault(self, faultSetting):
        # 0 : 1 fault
        # 1 : 2 faults
        # 2 : 4 faults
        # 3 : 6 faults
        self._injectConfig(0, 3, 2, faultSetting)

    def setAlertMode(self, mode):
        # 0 : Comparator Mode (like a thermostat)
        # 1 : Interrupt Mode
        self._injectConfig(0, 1, 1, mode)

    def setBoundTemp(self, upper, temperature):
        if self._ext and temperature > 150:
            temperature = 150
        elif temperature < -55:
            temperature = -55
        data = self._tempToBytes(temperature)
        reg = T_HIGH_REG if upper else T_LOW_REG
        self.bus.transaction(
            i2c.writing_bytes(self.address, reg, data[0], data[1]))

    def getBoundTemp(self, upper):
        reg = T_HIGH_REG if upper else T_LOW_REG
        res = self.bus.transaction(
            i2c.writing_bytes(self.address, reg),
            i2c.reading(self.address, 2))
        return self._bytesToTemp(res[0])

def test():
    with i2c.I2CMaster() as bus, \
         pi_header_1.pin(ALERT_PIN, direction=In, interrupt=Both, pull=PullUp) as alrt_pin, \
         Selector() as selector:
        selector.add(alrt_pin)
        tmp102 = TMP102(bus)
        tmp102.setExtendedMode(1)
        tmp102.setConversionRate(0)
        tmp102.setBoundTemp(False, 17.0)
        tmp102.setBoundTemp(True, 19.0)
        print(tmp102.getBoundTemp(False))
        print(tmp102.getBoundTemp(True))
        #tmp102.setAlertMode(1)
        tmp102.setFault(1)
        while True:
            selector.wait(30)
            if selector.ready is alrt_pin and selector.has_input:
                print("ALERT: alert_pin:", alrt_pin.value)
            temp = tmp102.readTemperature()
            alrt = tmp102.alert()
            print("Temperature: {0:6}C, alert: {1}, alert_pin: {2}".format(temp, alrt, alrt_pin.value))

if __name__ == "__main__":
    test()
