"""
    The Adafruit LCD Plate consists of a MCP23017 i2c port expander
    connected to a Hitachi HD44780 LCD controller as well as some LEDs
    and buttons. The mapping of the MCP23017 ports is as follows:
    GPIOA:
        0 - SELECT button
        1 - RIGHT button
        2 - DOWN button
        3 - UP button
        4 - LEFT button
        5 - unused
        6 - BACKLIGHT
        7 - GREEN LED
    GPIOB:
        0 - RED LED
        1 - HD44780 B7
        2 - HD44780 B6
        3 - HD44780 B5
        4 - HD44780 B4
        5 - HD44780 Clock (Enable)
        6 - HD44780 Read/Write (R/W)
        7 - HD44780 Register Select (RS)
"""
import quick2wire.i2c as i2c
from quick2wire.parts.mcp23017 import MCP23017
from quick2wire.parts.mcp23x17 import IODIRA, IODIRB, IPOLA, GPPUA, GPIOA, GPIOB, GPINTENA, INTCONA
from quick2wire.gpio import pi_header_1, In, Falling, PullUp
from time import sleep

# button bits on GPIOA
SELECT  = 0
RIGHT   = 1
DOWN    = 2
UP      = 3
LEFT    = 4
BUTTON_MASK = 0b00111111 # includes 6th unused input

# LEDs (bank,pin)
BACKLIGHT_PIN = (0,6)
GREEN_PIN = (0,7)
RED_PIN = (1,0)

# MCP23017 PortB control lines for HD44780
HD44780_RS = 0b10000000 # Register Select (RS)
HD44780_RW = 0b01000000 # R/W=0: Write, R/W=1: Read
HD44780_EN = 0b00100000 # Clock (Enable). Falling edge triggered
HD44780_D4 = 0b00010000
HD44780_D5 = 0b00001000
HD44780_D6 = 0b00000100
HD44780_D7 = 0b00000010
HD44780_LED =0b00000001 # Red LED

# LCD Commands
LCD_CLEARDISPLAY        = 0x01
LCD_RETURNHOME          = 0x02
LCD_ENTRYMODESET        = 0x04
LCD_DISPLAYCONTROL      = 0x08
LCD_CURSORSHIFT         = 0x10
LCD_FUNCTIONSET         = 0x20
LCD_SETCGRAMADDR        = 0x40
LCD_SETDDRAMADDR        = 0x80

# Flags for entry mode (LCD_ENTRYMODESET)
LCD_ENTRYSHIFTCURSOR    = 0x00
LCD_ENTRYSHIFTDISPLAY   = 0x01
LCD_ENTRYINCREMENT      = 0x02
LCD_ENTRYDECREMENT      = 0x00

# Flags for display on/off control (LCD_DISPLAYCONTROL)
LCD_DISPLAYON           = 0x04
LCD_DISPLAYOFF          = 0x00
LCD_CURSORON            = 0x02
LCD_CURSOROFF           = 0x00
LCD_BLINKON             = 0x01
LCD_BLINKOFF            = 0x00

# Flags for display/cursor shift (LCD_CURSORSHIFT)
LCD_DISPLAYMOVE = 0x08
LCD_CURSORMOVE  = 0x00
LCD_MOVERIGHT   = 0x04
LCD_MOVELEFT    = 0x00

LCD_ROW2_ADDRESS = 0x40

# PI GPIO pin carrying interrupt for buttons
BUTTON_INTERRUPT_PIN = 26

def get_interrupt_pin():
    """ Button interrupts are fed back to PI header via a GPIO pin.
        Since MCP23017 runs at 5V, and PI GPIOs take 3V3, need to set
        MCP23017 interrupts as "open drain" and use an internal pullup
        on the PI input pin.
    """
    return pi_header_1.pin(
        BUTTON_INTERRUPT_PIN,
        direction=In,
        interrupt=Falling,
        pull=PullUp)

class Adafruit_LCDPlate(object):
    def __init__(self, master, address=0x20):
        self.chip = MCP23017(master, address)
        self.chip.reset(interrupt_polarity=0,
                        interrupt_open_drain=True, # interrupt pin feeds 3V3 input on Pi with internal pullup
                        interrupt_mirror=False) # want interrupts only on port with buttons
        regs = self.chip.registers
        regs.write_register(IODIRA, 0b00111111) # 2 LEDs=outputs, buttons=inputs
        #regs.write_register(IODIRB, 0b00010000) # LCD D7=input, other LCD pins=output, LED=output
        regs.write_register(IODIRB, 0b00000000) # All output
        regs.write_register(IPOLA, 0b00111111) # Invert polarity on button inputs
        regs.write_register(GPPUA, 0b00111111) # Enable pull-ups on buttons
        # enable interrupt-on-pin change for buttons
        regs.write_register(GPINTENA, 0b00011111)
        # interrupt when change from previous value (not DEFVALA)
        regs.write_register(INTCONA, 0b00000000)

        self.cached_led_pin = 0 # cache LED value which saves us from having to read GPIOB

        """
        The HD44780 starts in 8 bit mode. The 4-Bit mode initialization sequence is
        0b0011xxxx, 0b0011xxxx, 0b0011xxxx, 0b0010xxxx, 0b0010NFxx.
        Since our write function assumes 4bit mode and sends nibbles,
        this equates to 0x33, 0x32, 0x28.
        """
        self.write8(0x33)
        self.write8(0x32)
        self.write8(LCD_FUNCTIONSET | 0x08) #( | DL=0 (4bit) | N=1 (2 lines) | F=0 (5x8)
        self.reset()

    def reset(self):
        self.entrymode = LCD_ENTRYSHIFTCURSOR | LCD_ENTRYINCREMENT

        self.displaycontrol = (LCD_DISPLAYON |
                               LCD_CURSOROFF |
                               LCD_BLINKOFF)

        self.write8(LCD_CLEARDISPLAY)
        self.write8(LCD_RETURNHOME)
        self.write8(LCD_ENTRYMODESET   | self.entrymode)
        self.write8(LCD_DISPLAYCONTROL | self.displaycontrol)

    flip = (0b00000000, 0b00010000, 0b00001000, 0b00011000,
            0b00000100, 0b00010100, 0b00001100, 0b00011100,
            0b00000010, 0b00010010, 0b00001010, 0b00011010,
            0b00000110, 0b00010110, 0b00001110, 0b00011110)

    def write4(self, value, mask=0):
        v = self.flip[value] | self.cached_led_pin | mask
        self.chip.registers.master.transaction(
            i2c.writing_bytes(self.chip.registers.address, GPIOB, v | HD44780_EN))
        self.chip.registers.master.transaction(
            i2c.writing_bytes(self.chip.registers.address, GPIOB, v))

    time_consuming = ( LCD_CLEARDISPLAY, LCD_RETURNHOME )
    def write8(self, value):
        self.write4(value >> 4)
        self.write4(value & 0x0F)
        if value in self.time_consuming:
            sleep(0.015)

    def write_char(self, value):
        self.write4(ord(value) >> 4, HD44780_RS)
        self.write4(ord(value) & 0x0F, HD44780_RS)

    def write_text(self, text):
        for c in text:
            self.write_char(c)

    # Utility methods
    def set_led(self, led, enable):
        self.chip.bank(led[0]).pin(led[1]).value = enable
        if led[0] == 1: # portB
            self.cached_led_pin = HD44780_LED if enable else 0

    def set_backlight(self, enable):
        self.set_led(BACKLIGHT_PIN, not enable)

    def button_pressed(self, button):
        return self.chip.bank(0).pin(1 << button)

    def get_button_state(self):
        return self.chip.registers.read_register(GPIOA) & 0b00111111

    def button_state_pressed(self, button_state, button):
        return button_state & (1 << button)

    def clear(self):
        self.write8(LCD_CLEARDISPLAY)

    def home(self):
        self.write8(LCD_RETURNHOME)

    row_offsets = ( 0x00, 0x40, 0x14, 0x54 )
    def set_cursor_pos(self, col, row):
        if row > 2:
            row = 1
        elif row < 0:
            row = 0
        self.write8(LCD_SETDDRAMADDR | (col + self.row_offsets[row]))

    def set_display(self, enable):
        """ Turn the display on or off (quickly) """
        if enable:
            self.displaycontrol |= LCD_DISPLAYON
        else:
            self.displaycontrol &= ~LCD_DISPLAYON
        self.write8(LCD_DISPLAYCONTROL | self.displaycontrol)

    def set_cursor(self, enable):
        """ Underline cursor on or off """
        if enable:
            self.displaycontrol |= LCD_CURSORON
        else:
            self.displaycontrol &= ~LCD_CURSORON
        self.write8(LCD_DISPLAYCONTROL | self.displaycontrol)

    def set_blink(self, enable):
        """ Turn the blinking cursor on or off """
        if enable:
            self.displaycontrol |= LCD_BLINKON
        else:
            self.displaycontrol &= ~LCD_BLINKON
        self.write8(LCD_DISPLAYCONTROL | self.displaycontrol)

    def scroll_display(self, left):
        """ These commands scroll the display without changing the RAM """
        self.write8(LCD_CURSORSHIFT | LCD_DISPLAYMOVE |\
                 (LCD_MOVELEFT if left else LCD_MOVERIGHT))

    def left_to_right(self):
        """ This is for text that flows left to right """
        self.entrymode |= LCD_ENTRYINCREMENT
        self.write8(LCD_ENTRYMODESET | self.entrymode)

    def right_to_left(self):
        """ This is for text that flows right to left """
        self.entrymode &= ~LCD_ENTRYINCREMENT
        self.write8(LCD_ENTRYMODESET | self.entrymode)

    def set_autoscroll(self, enable):
        """ This will 'right justify' or 'left justify' text from the cursor """
        if enable:
            self.entrymode |= LCD_ENTRYSHIFTDISPLAY
        else:
            self.entrymode &= ~LCD_ENTRYSHIFTDISPLAY
        self.write8(LCD_ENTRYMODESET | self.entrymode)

    def create_char(self, location, bitmap):
        self.write8(LCD_SETCGRAMADDR | ((location & 7) << 3))
        self.write_text(bitmap)
        self.write8(LCD_SETDDRAMADDR)

    def message(self, text):
        """ Send string to LCD. Newline wraps to second line"""
        lines = str(text).split('\n')
        for i, line in enumerate(lines):
            if i > 0:
                self.write8(LCD_SETDDRAMADDR | LCD_ROW2_ADDRESS) # set DDRAM address to 2nd line
            self.write_text(line)    # one line

    #@staticmethod
    #def get_interrupt_pin(): moved to module scope

##################################
### Various tests and examples ###
##################################
if __name__ == "__main__":
    from quick2wire.selector import Selector

    def message_with_scroll(lcd, text, repeat=2):
        lcd.reset()
        lines = str(text).split('\n')
        cols = 0
        for i, line in enumerate(lines):
            if i > 0:
                lcd.set_cursor_pos(0, 1)
            lcd.write_text(line)
            cols = max(cols, len(line))
        if cols > 16:
            for i in range(repeat):
                if i > 0:
                    sleep(1)
                lcd.home()
                sleep(1)
                for j in range(16, cols):
                    if j > 16:
                        sleep(0.4)
                    lcd.scroll_display(True)

    def slow_type_from_left(lcd, text):
        lcd.reset()
        lines = str(text).split('\n')
        for i, line in enumerate(lines):
            if i > 0:
                lcd.set_cursor_pos(0, 1)
            for j, c in enumerate(line):
                if j > 0:
                    sleep(0.2)
                lcd.write_text(c)

    def slow_type_from_left_with_scroll(lcd, text):
        lcd.reset()
        lines = str(text).split('\n')
        for i, line in enumerate(lines):
            if i > 0:
                lcd.home()
                lcd.set_cursor_pos(0, 1)
                lcd.set_autoscroll(False)
            for j, c in enumerate(line):
                if j > 0:
                    sleep(0.2)
                if j == 16:
                    lcd.set_autoscroll(True)
                lcd.write_text(c)

    def slide_in_from_right(lcd, text, park=True):
        lcd.reset()
        lines = str(text).split('\n')
        lcd.set_cursor_pos(16, 0) # just off the display
        lcd.write_text(lines[0])
        cols = len(lines[0])
        if len(lines) > 1:
            lcd.set_cursor_pos(16, 1) # just off the display
            lcd.write_text(lines[1])
            cols = max(cols, len(lines[1]))
        if park:
            cols = max(16, cols)
        else:
            cols = 16 + cols
        for i in range(cols):
            if i > 0:
                sleep(0.2)
            lcd.scroll_display(True)

    with i2c.I2CMaster() as master:
        lcd = Adafruit_LCDPlate(master)

        lcd.message("It's\nworking!")
        sleep(5)
        lcd.set_backlight(False)

        for p in [GREEN_PIN, RED_PIN]:
            lcd.set_led(p, True)
            sleep(1)
            lcd.set_led(p, False)
            sleep(1)

        lcd.set_backlight(True)

        lcd.clear()
        lcd.message("Push some\nbuttons...")

        with Selector() as selector, get_interrupt_pin() as int_pin:
            selector.add(int_pin)
            for i in range(10):
                selector.wait(10)
                if selector.ready is int_pin:
                    print("Button state: 0x{0:02x}, 0b{0:08b}".format(lcd.get_button_state()))

        """
        while True:
            print("GPIOA: 0x{0:02x}, 0b{0:08b}".format(lcd.chip.registers.read_register(GPIOA)))
            print("GPIOB: 0x{0:02x}, 0b{0:08b}".format(lcd.chip.registers.read_register(GPIOB)))
            sleep(1)
        """

        slow_type_from_left(lcd, "Mary had a\nlittle lamb!")
        sleep(5)
        slide_in_from_right(lcd, "Mary had a\nlittle lamb!")
        sleep(5)
        slow_type_from_left_with_scroll(lcd, "This is a very long line that won't fit\nwithout scrolling the display")
        sleep(5)

        lcd.clear()
        lcd.set_backlight(False)
        lcd.set_led(GREEN_PIN, False)
        lcd.set_led(RED_PIN, False)
