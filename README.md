# auto-pump
runs a pump until it goes dry. Also sends info to influxdb


## Hardware Assumptions

You're running an ESP32. We use [this WROVER one](https://www.aliexpress.com/item/4000064597840.html?spm=a2g0s.9042311.0.0.70504c4dpiaF4W)

You have a powerful enough relay/contactor to flip on your "pump". We use these:
  -  [380V 25A relay](https://www.amazon.com/gp/product/B074FT4VXB)

Other Electronics
  - [ACS712 Current sensor](https://www.amazon.com/HiLetgo-ACS712-Current-Sensor-Module/dp/B07SPRL8DL)
  - [TM1637 4 segment LCD](https://www.amazon.com/gp/product/B01DKISMXK)
  - [Encoder knob](https://www.amazon.com/gp/product/B07DM2YMT4)



## Python Modules
  - https://github.com/mcauser/micropython-tm1637
  - https://github.com/infinite-tree/micropython-encoder-knob


## Building & Installing


### MicroPython Environment Installation

Download [micropython (spiram-idf4) here](https://micropython.org/download/esp32/)

```
sudo apt get install esptool
esptool.py --port /dev/ttyUSB0 erase_flash
esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash --flash_size=detect 0x1000 ./micro-python/esp32spiram-idf4-20191220-v1.12.bin
```


### Installing the Python code

```
pip3 install adafruit-ampy --upgrade
ampy --port /dev/ttyUSB0 put config.py /config.py
ampy --port /dev/ttyUSB0 put tm1637 /tm1637.py
ampy --port /dev/ttyUSB0 put encoder.py /encoder.py
ampy --port /dev/ttyUSB0 put main.py /main.py

