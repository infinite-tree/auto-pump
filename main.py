import gc
import machine
from micropython import const
import network
import _thread
import time
import ubinascii
import ujson
import urequests

# https://github.com/mcauser/micropython-tm1637
import tm1637


# https://github.com/infinite-tree/micropython-encoder-knob
from encoder import EncoderKnob


###
### Debug or Production
PRODUCTION = 1
###
###


#
# Macros
#
LCD_DIO_PIN = const(12)
LCD_CLK_PIN = const(13)

PUMP_AMP_PIN = const(32)

ENC_BTN_PIN = const(4)
ENC_CLK_PIN = const(18)
ENC_DATA_PIN = const(19)

AUTO_SWITCH_PIN = const(21)
PUMP_PWR_PN = const(25)

# value of 1 = normal, set to 0 if the switch is inverted
SWITCH_AUTO = const(1)
# 1 sec
ADC_READ_DURATION = const(1100)
DATA_RETRY_COUNT = const(3)
# 15sec
DATA_DELAY_SEC = const(15)
CONNECTION_DELAY_SEC = const(10)
AUTO_READ_DELAY = const(30)

CONFIG_WIFI_SSID = "WIFI_SSID"
CONFIG_WIFI_PASSWD = "WIFI_PASSWD"

CONFIG_INFLUXDB_URL = "INFLUXDB_URL"
CONFIG_INFLUXDB_USER = "INFLUXDB_USER"
CONFIG_INFLUXDB_PASSWD = "INFLUXDB_PASSWD"

CONFIG_SENSOR_NAME = "SENSOR_NAME"
CONFIG_SENSOR_LOCATION = "SENSOR_LOCATION"

PUMP_MEASUREMENT = "pump_load"
PUMP_MODE = "pump_mode"

DEFAULT_AUTO_PUMP_THRESHOLD = 60
DEFAULT_AUTO_WATER_LOAD = 2000


#
# Globals
#
WIFI = network.WLAN(network.STA_IF)
WIFI.active(True)


#
# Functions
#
def loadConfig():
    print("Loading Config file")
    import config
    # there was a bug about memory needing to be in ram
    # createing anew dict from the one in flash should do it..
    return dict(config.config)


def saveConfig(config):
    with open("/config.py", "w") as f:
        f.write("config = %s"%config)


def connectToWifi(config):
    while not WIFI.isconnected():
        ssid = config.get(CONFIG_WIFI_SSID)
        passwd = config.get(CONFIG_WIFI_PASSWD)
        print("Connecting to {}".format(ssid))
        WIFI.connect(ssid, passwd)
        for x in range(CONNECTION_DELAY_SEC):
            if WIFI.isconnected():
                print("Connected:")
                print(WIFI.ifconfig())
                return
            else:
                print(".")
                time.sleep(1)


def sendDatapoint(config, measurement, value):
    connectToWifi(config)
    url = config.get(CONFIG_INFLUXDB_URL)
    auth = ubinascii.b2a_base64("{}:{}".format(config.get(CONFIG_INFLUXDB_USER), config.get(CONFIG_INFLUXDB_PASSWD)))
    headers = {
        'Content-Type': 'text/plain',
        'Authorization': 'Basic ' + auth.decode().strip()

    }
    data = "{},location={},sensor={} value={:.2}".format(measurement,
                                                         config.get(CONFIG_SENSOR_LOCATION),
                                                         config.get(CONFIG_SENSOR_NAME),
                                                         value)
    
    for x in range(DATA_RETRY_COUNT):
        try:
            r = urequests.post(url, data=data, headers=headers)
        except:
            print("Error sending data")
            break
        
        if len(r.text) < 1:
            return True
        else:
            print("Failed to send data")
            print(r.json())
            # The server said no, so no need to retry
            return False
    
    return False


class Pump(object):
    def __init__(self):
        self.PumpSensor = machine.ADC(machine.Pin(PUMP_AMP_PIN))
        self.PumpSensor.atten(machine.ADC.ATTN_11DB)
        self.PumpSensor.width(machine.ADC.WIDTH_12BIT)

        self.StartTime = 0

        self.PumpPower = machine.Pin(PUMP_PWR_PN, machine.Pin.OUT)
        self.off()

    def on(self):
        self.PumpPower.on()
        self.StartTime = time.time()
    
    def off(self):
        self.PumpPower.off()
        self.StartTime = 0

    def isOn(self):
        return self.PumpPower.value() == 1

    def getElapsedTime(self):
        if self.isOn():
            return time.time() - self.StartTime
        return 0
    
    def getLoad(self):
        # It looks like for ADC reading we need to measure the swing
        low = self.PumpSensor.read()
        high = low

        now = time.ticks_ms()
        end_time = now + ADC_READ_DURATION
        while now < end_time:
            r = self.PumpSensor.read()
            low = min(low, r)
            high = max(high, r)
            now = time.ticks_ms()
            time.sleep(0.01)
        return high-low


class AutoPump(object):
    MODE_AUTO_STANDBY = 0
    MODE_AUTO_PUMPING = 1

    MODE_TIMER_STANDBY = 2
    MODE_TIMER_PUMPING = 3

    AUTO_MODE_MENU = 0
    AUTO_MENU_RUN = 0
    AUTO_CALIB_THRESHOLD = 1
    AUTO_CALIB_WATER = 2

    def __init__(self, config, pump):
        self.Config = config
        self.Pump = pump
        
        self.Switch = machine.Pin(AUTO_SWITCH_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
        self.Switch.irq(handler=self.handleSwitch, trigger=machine.Pin.IRQ_RISING | machine.Pin.IRQ_FALLING)

        self.EncoderKnob = EncoderKnob(ENC_CLK_PIN,
                                       ENC_DATA_PIN,
                                       btn_pin=ENC_BTN_PIN,
                                       rotary_callback=self.handleKnob,
                                       btn_callback=self.handleButton)
        
        self.Display = tm1637.TM1637(clk=machine.Pin(LCD_CLK_PIN), dio=machine.Pin(LCD_DIO_PIN))
        self.Display.show("LOAD")
        time.sleep(1.5)
        self.Display.show("DONE")

        self.Mode = self.MODE_AUTO_STANDBY
        self.LastDataUpdate = 0
        self.PumpStartTime = 0
        self.RemainingPumpMinutes = 0
        self.SecondsToPump = 0

        self.AutoThreshold = self.Config.get("auto_threshold", DEFAULT_AUTO_PUMP_THRESHOLD)
        self.AutoWaterLoad = self.Config.get("auto_water_load", DEFAULT_AUTO_WATER_LOAD)

        self.AutoMode = self.AUTO_MODE_MENU
        self.AutoMenu = ["AUTO", "RATO", "CALR"]
        self.AutoMenuSelected = 0

        # figure 8: a,b,g,e,d,c,g,f
        self.AutoAnimation = [0b00000001, 0b00000010, 0b01000000, 0b00010000, 0b00001000, 0b00000100, 0b01000000, 0b00100000]
        self.AutoAnimationIdx = 0

        # force the mode handler
        self.handleSwitch(None)

    def handleSwitch(self, pin):
        # Always stop the pump when the mode changes
        self.Pump.off()

        pos = self.Switch.value()
        print("mode switch is now: %d"%pos)

        if self.Switch.value() != SWITCH_AUTO:
            self.RemainingPumpMinutes = self.EncoderKnob.value(0)
            self.Display.number(self.RemainingPumpMinutes)
            self.Mode = self.MODE_TIMER_STANDBY
        else:
            # TODO: once implemented
            self.Display.show('AUTO')
            self.Mode = self.MODE_AUTO_STANDBY
            
    def handleKnob(self, change):
        # print("knob changed: %d"%change)

        #
        # Timer Modes
        #
        if self.Mode == self.MODE_TIMER_STANDBY:
            value = self.EncoderKnob.value()
            if value < 0:
                value = self.EncoderKnob.value(0)
            self.RemainingPumpMinutes = value
            self.Display.number(self.RemainingPumpMinutes)
            # print("TIMER_STANDBY")
        # Do nothing in pumping mode

        #
        # Auto Modes
        #
        elif self.Mode == self.MODE_AUTO_STANDBY:
            # In standby mode, show the menu or calibration screens
            if self.AutoMode == self.AUTO_MODE_MENU:
                # change the shown menu option
                value = self.EncoderKnob.value()
                self.AutoMenuSelected = value % len(self.AutoMenu)
                self.Display.show(self.AutoMenu[self.AutoMenuSelected])
            
            elif self.AutoMode == self.AUTO_CALIB_THRESHOLD:
                # change the threshold calibration
                value = self.EncoderKnob.value()
                if value < 0:
                    value = self.EncoderKnob.value(0)
                if value > 100:
                    value = self.EncoderKnob.value(100)

                self.AutoThreshold = value                
                self.Display.number(self.AutoThreshold)
            
            elif self.AutoMode == self.AUTO_CALIB_WATER:
                # knob rotates should have no effect here
                return
        
        elif self.Mode == self.MODE_AUTO_PUMPING:
            # knob rotates should have no effect here
            return


    def handleButton(self):
        # Button was pressed
        print("Button Pressed")

        #
        # Timer Modes
        #
        if self.Mode == self.MODE_TIMER_STANDBY:
            if self.RemainingPumpMinutes > 0:
                # Button press when in standby and their is at least 1 minute on the clock turns the
                # pump on and moves to pumpming mode
                self.Mode = self.MODE_TIMER_PUMPING
                self.SecondsToPump = self.RemainingPumpMinutes * 60
                self.Pump.on()
                self.PumpStartTime = time.time()
                self.Display.brightness(7)
                print("Pump started")
            else:
                print("Minutes: %d"%self.RemainingPumpMinutes)
        elif self.Mode == self.MODE_TIMER_PUMPING:
            # If the pump is pumping, then the button stops the pump and moves back into standby mode 
            self.Mode = self.MODE_TIMER_STANDBY
            self.Pump.off()
            self.RemainingPumpMinutes = self.EncoderKnob.value(0)
            self.Display.number(self.RemainingPumpMinutes)
            self.Display.brightness(7)
            print("STOPPED")
        
        #
        # Auto Modes
        #
        elif self.Mode == self.MODE_AUTO_STANDBY:
            if self.AutoMode == self.AUTO_MODE_MENU:
                # Handle Top level menu items
                if self.AutoMenuSelected == self.AUTO_MENU_RUN:
                    # Start the pup only if the first aka "RUN" option is selected
                    self.Mode = self.MODE_AUTO_PUMPING
                    self.Pump.on()
                    self.PumpStartTime = time.time()
                    print("Auto pumping")
                    self.Display.show(" ON ")

                elif self.AutoMenuSelected == self.AUTO_CALIB_THRESHOLD:
                    # enter threshold calibration
                    value = self.EncoderKnob.value(self.AutoThreshold)
                    self.Display.number(value)
                    self.AutoMode = self.AUTO_CALIB_THRESHOLD
                elif self.AutoMenuSelected == self.AUTO_CALIB_WATER:
                    #FIXME: start the water calibration mode
                    self.AutoMode = self.AUTO_CALIB_WATER
            
            elif self.AutoMode == self.AUTO_CALIB_THRESHOLD:
                # In threshold calibartion mode. pushing the button saves the changes and goes back to the menu
                self.Config["auto_threshold"] = self.AutoThreshold
                saveConfig(self.Config)
                print("auto threshold saved")
                
                self.AutoMode = self.AUTO_MODE_MENU
                value = self.EncoderKnob.value(self.AutoMenuSelected)
                self.Display.show(self.AutoMenu[value])
            
            elif self.AutoMode == self.AUTO_CALIB_WATER:
                # Calibrating load when pumping water
                # Pressing the button saves the current value as the water load
                self.Config["auto_water_load"] = self.AutoWaterLoad
                saveConfig(self.Config)
                print("water load saved")

                self.AutoMode = self.AUTO_MODE_MENU
                self.Display.show(self.AutoMenu[self.AutoMenuSelected])

        elif self.Mode == self.MODE_AUTO_PUMPING:
            print("pump off")
            self.Pump.off()
            self.Display.show(self.AutoMenu[0])
        time.sleep(0.5)

    def checkLoadToStop(self, load):
        percent = float(load) / self.AutoWaterLoad * 100
        # print("DEBUG: saved water load: ", self.AutoWaterLoad)
        # print("DEBUG: current load: ", load)
        # print("DEBUG: percent: ", percent)
        # print("DEBUG: threshold: ", self.AutoThreshold)
        if percent < self.AutoThreshold:
            return True
        return False

    def _networkThread(self):
        while True:
            try:
                connectToWifi(self.Config)

                while True:
                    now = time.time()
                    # send updates
                    if now - self.LastDataUpdate > DATA_DELAY_SEC:
                        pump_value = float(self.Pump.getLoad())
                        print("pump: %f"%pump_value)
                        sendDatapoint(self.Config, PUMP_MEASUREMENT, pump_value)
                        sendDatapoint(self.Config, PUMP_MODE, self.Mode)

                        self.LastDataUpdate = now
                    
                    time.sleep(1)
            except Exception as e:
                print("Network Thread error:", e)

    def _run(self):
        now = time.time()

        #
        # Update the display
        #
        if self.Mode == self.MODE_TIMER_PUMPING:
            # update the minutes
            elapsed_seconds = now - self.PumpStartTime
            self.RemainingPumpMinutes = int((self.SecondsToPump + 59 - elapsed_seconds)/60)
            self.EncoderKnob.value(self.RemainingPumpMinutes)
            self.Display.number(self.RemainingPumpMinutes)

            # Turn off the pump if its done
            if elapsed_seconds >= self.SecondsToPump:
                self.Pump.off()
                self.Mode = self.MODE_TIMER_STANDBY
                self.Display.brightness(7)
                self.RemainingPumpMinutes = self.EncoderKnob.value(0)
                self.Display.number(self.RemainingPumpMinutes)
                print("Pumping done")
        
        elif self.Mode == self.MODE_TIMER_STANDBY:
            # Flash the display by adjusting the brightness while also minimizing refresh delays
            if now % 2:
                self.Display.brightness(7)
                self.Display.number(self.RemainingPumpMinutes)
            else:
                self.Display.brightness(2)
                # self.Display.number(self.RemainingPumpMinutes)

        elif self.Mode == self.MODE_AUTO_STANDBY:
            # 3 modes: pump on, cailb water, calib threshold %
            # default to 60% of water = off
            if self.AutoMode == self.AUTO_MODE_MENU:
                self.Display.show(self.AutoMenu[self.AutoMenuSelected])
            elif self.AutoMode == self.AUTO_CALIB_THRESHOLD:
                self.Display.number(self.AutoThreshold)
            elif self.AutoMode == self.AUTO_CALIB_WATER:
                if now % 5:
                    self.AutoWaterLoad = self.Pump.getLoad()
                self.Display.number(self.AutoWaterLoad)

        elif self.Mode == self.MODE_AUTO_PUMPING:
            # Handle the auto pumping logic
            if now - self.PumpStartTime > AUTO_READ_DELAY:
                if now % 10:
                    if self.checkLoadToStop(self.Pump.getLoad()):
                        self.Pump.off()
                        self.Mode = self.MODE_AUTO_STANDBY
                        self.Display.show(self.AutoMenu[0])
                        print("auto pumping completed")

            # update the display
            if now % 2:
                self.Display.brightness(7)
                elapsed_seconds = now - self.PumpStartTime
                num = '{:>3}'.format(str(int(elapsed_seconds/60)))
                seg = self.Display.encode_string(num)
                segments = bytearray(4)
                segments[0] = self.AutoAnimation[self.AutoAnimationIdx]
                for x in range(3):
                    segments[1+x] = seg[x]

                self.AutoAnimationIdx += 1
                if self.AutoAnimationIdx >= len(self.AutoAnimation):
                    self.AutoAnimationIdx -= len(self.AutoAnimation)
                self.Display.write(segments, 0)

        time.sleep(0.1)


    def run(self):
        _thread.start_new_thread(self._networkThread, tuple())
        while True:
            self._run()



def main():
    config = loadConfig()
    pump = Pump()
    a = AutoPump(config, pump)
    gc.enable()
    gc.collect()
    a.run()


if PRODUCTION == 1:
    try:
        main()
    except Exception as e:
        machine.reset()
else:
    print("PRODUCTION = 0")
