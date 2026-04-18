# /*****************************************************************************
# * | File        :	  epdconfig.py
# * | Author      :   Waveshare team
# * | Function    :   Hardware underlying interface
# * | Info        :
# *----------------
# * | This version:   V1.0
# * | Date        :   2019-06-21
# * | Info        :   
# ******************************************************************************
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

import os
import sys
import time
import spidev
import smbus
import logging
import numpy as np
from gpiozero import *

class RaspberryPi:
    def __init__(self,spi=spidev.SpiDev(0,0),spi_freq=40000000,rst = 27,dc = 25,bl = 18,tp_int = 4,tp_rst = 17,bl_freq=1000): 
        self.np=np

        self.INPUT = False
        self.OUTPUT = True

        self.SPEED  =spi_freq
        self.BL_freq=bl_freq   

        self.RST_PIN= rst
        self.DC_PIN = dc
        self.BL_PIN = bl

        self.TP_INT = tp_int
        self.TP_RST = tp_rst

        self.X_point = self.Y_point = self.Gestures = 0

        #Initialize SPI
        self.SPI = spi
            
        # #Initialize I2C
        self.I2C = smbus.SMBus(1)
        self.address = 0x15

    def gpio_mode(self,Pin,Mode,pull_up = None,active_state = True):
        if Mode:
            return DigitalOutputDevice(Pin,active_high = True,initial_value =False)
        else:
            return DigitalInputDevice(Pin,pull_up=pull_up,active_state=active_state)

    def digital_write(self, Pin, value):
        if value:
            Pin.on()
        else:
            Pin.off()

    def digital_read(self, Pin):
        return Pin.value

    def delay_ms(self, delaytime):
        time.sleep(delaytime / 1000.0)

    def spi_writebyte(self, data):
        if self.SPI!=None :
            self.SPI.writebytes(data)

    def Touch_module_init(self):
        self.GPIO_TP_INT = Button(self.TP_INT)
        self.GPIO_TP_RST = self.gpio_mode(self.TP_RST,self.OUTPUT)
        # pass
        # self.GPIO.setup(self.TP_INT,    self.GPIO.IN,self.GPIO.PUD_UP)
        # self.GPIO.setup(self.TP_RST,    self.GPIO.OUT)

    def i2c_write_byte(self, Addr, val):
        self.I2C.write_byte_data(self.address, Addr, val)

    def i2c_read_byte(self,Addr):
        return self.I2C.read_byte_data(self.address, Addr)

    def gpio_pwm(self,Pin):
        return PWMOutputDevice(Pin,frequency = self.BL_freq)
        
    def bl_DutyCycle(self, duty):
        self.GPIO_BL_PIN.value = duty / 100
        
    def bl_Frequency(self,freq):
        self.GPIO_BL_PIN.frequency = freq

    def LCD_module_init(self):
        self.GPIO_RST_PIN= self.gpio_mode(self.RST_PIN,self.OUTPUT)
        self.GPIO_DC_PIN = self.gpio_mode(self.DC_PIN,self.OUTPUT)
        self.GPIO_BL_PIN = self.gpio_pwm(self.BL_PIN)
        self.bl_DutyCycle(0)
        
        if self.SPI!=None :
            self.SPI.max_speed_hz = self.SPEED        
            self.SPI.mode = 0b00    

        return 0

    def LCD_module_exit(self):
        logging.debug("spi end")
        if self.SPI!=None :
            self.SPI.close()   
        
        logging.debug("gpio cleanup...")
        self.GPIO_RST_PIN.close()
        self.GPIO_DC_PIN.close()       
        self.GPIO_BL_PIN.close()      
        #self.GPIO.cleanup()

    def Touch_module_exit(self):
        if self.I2C!=None :
            self.I2C.close()
        self.GPIO_TP_RST.close()
        self.GPIO_TP_INT.close()
        time.sleep(0.001)

'''
if os.path.exists('/sys/bus/platform/drivers/gpiomem-bcm2835'):
    implementation = RaspberryPi()

for func in [x for x in dir(implementation) if not x.startswith('_')]:
    setattr(sys.modules[__name__], func, getattr(implementation, func))
'''

### END OF FILE ###
