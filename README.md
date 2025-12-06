# UFO Shutter Controller (Fireball Re-use)

This project repurposes the **Fireball weather balloon “UFO” shutter** and its
original **UFO Shutter Controller** card as a stand-alone, Arduino-controlled
24 V shutter system.

The Arduino acts as a USB I/O card, and all high-current coil driving is still
handled by the original shutter controller board exactly as it was in the
fireball computer.

---

## Overview

**Goal:**  
Use the legacy Fireball UFO shutter in a new experiment with:

- A standard **24 V DC power supply**
- The original **UFO Shutter Controller** card
- An **Arduino Uno** speaking Firmata
- A small **Python script (`ufo_shutter.py`) using pyFirmata** to open/close
  the shutter on command

The shutter coils are still driven by the OEM controller at 24 V; the Arduino
only generates a TTL-level control signal.

---

## Hardware

### Components

- UFO shutter assembly (2-coil solenoid shutter)
- UFO Shutter Controller card  
  - Terminals:
    - **1** – +24 V input  
    - **2** – Ground (0 V)  
    - **7, 8** – Solenoid outputs (two coils in parallel)  
    - **10** – TTL signal input  
    - **11** – Signal ground
- 24 V DC power supply
- Arduino Uno (or compatible)
- 1 kΩ resistor (in series with the TTL input)
- Existing white LEMO cable between shutter and controller

### Shutter → Controller wiring (LEMO cable)

From the original documentation:

- White cable between UFO (spectrograph tank) and controller has **4 wires**:
  - **Red, black** → shutter solenoid 1
  - **Green, white** → shutter solenoid 2
- For solenoids wired in parallel:
  - **Red and green** → controller **pin 7**
  - **Black and white** → controller **pin 8**

See the original connector diagram for visual pinouts.

---

## Wiring

High-level connections:

1. **24 V Power**
   - 24 V PSU **+** → controller **pin 1**
   - 24 V PSU **–** → controller **pin 2**

2. **Controller ↔ Shutter**
   - As in the original system via the LEMO cable:
     - Coils wired in parallel between **pins 7 and 8**

3. **Arduino ↔ Controller (TTL)**
   - Arduino digital **D8** → **1 kΩ resistor** → controller **pin 10** (TTL in)
   - Arduino **GND** → controller **pin 11** (TTL ground)

> ⚠️ The Arduino never sees 24 V. Only the shutter controller and power
> supply operate at 24 V. The Arduino is just a 5 V logic source.

---

## Software

There are two main ways to drive the shutter:

1. **Python + pyFirmata + StandardFirmata on Arduino**  
   (interactive I/O from the host computer)
2. **Arduino test sketch** (`ufo_shutter_test.ino`)  
   (simple, fixed-timing standalone test to bypass any compiler/arduino/pyfirmata issues)

### 1. Arduino firmware (StandardFirmata)

To use the Python controller:

1. Open the **Arduino IDE**.
2. Load:  
   `File → Examples → Firmata → StandardFirmata`
3. Select the correct **board** and **port** for your Arduino.
4. **Upload** the sketch.

The Arduino is now a generic I/O device controlled over USB by pyFirmata.

---

## Python Environment

`ufo_shutter.py` is a small CLI tool that talks to the Arduino via pyFirmata.

### Virtual environment setup

From the project folder (e.g. `~/ufo_shutter`):

```bash
cd ~/Desktop/ufo_shutter

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install pyfirmata pyserial
```
> Python 3.13 note:
At the time of writing, pyfirmata may require a small patch for
Python ≥ 3.13:
in `site-packages/pyfirmata/pyfirmata.py`, replace
```bash
inspect.getargspec
```
with 
```bash
inspect.getfullargspec
```
at the line where it is used. (typically line 185) 


---

## Running the code 

**ufo_shutter.py** – Command-line Shutter Control

This script:
1. Connects to the Arduino on a given serial port
2. Configures D8 as a digital output
3. Lets you open/close the shutter or pulse it for a specified time

>Key configuration at the top of the script:

Default serial port and pin; override from the command line if needed
```bash
DEFAULT_PORT = '/dev/cu.usbmodem101'   # macOS example
SHUTTER_PIN_NUM = 8                    # D8 on Arduino

OPEN_STATE = 0    # logic level that OPENS the shutter (LOW in this setup)
CLOSED_STATE = 1  # logic level that CLOSES the shutter
```
you select ports for linux and windows using : 
```bash
DEFAULT_PORT = 'COM3'         #windows 
DEFAULT_PORT = '/dev/ttyACM0' #linux
```
You can use the Arduino IDE to check which port the board is connected to, or use Devic Manager on Windows. 

If your hardware is inverted (open actually closes), swap OPEN_STATE CLOSED_STATE.

```bash
OPEN_STATE = 1    # logic level that OPENS the shutter (HIGH in this setup)
CLOSED_STATE = 0  # logic level that CLOSES the shutter
```
**Running the Script**

#Activate the environment 
```bash
source .venv/bin/activate
```
You can call the script in two ways
# Option A: use the default port hardcoded in the script
```bash
python ufo_shutter.py
```

# Option B: specify the port explicitly as a parameter 
```bash
python ufo_shutter.py /dev/cu.usbmodem101
```

**CLI commands**

Once running, the script prints a small command menu:
```bash 
o → open shutter
c → close shutter
p <ms> → pulse open for <ms> milliseconds
q → quit
```

Example: p 500 = open for 500 ms, then close
If <ms> is omitted or invalid, defaults to 1000 ms


---

## Photos & Original Context

Additional images in this repo show:

The shutter controller in its original Fireball computer rack
(Ufo_Controller_orginal_position.HEIC)

Original wiring harnesses and terminal labels
(ufo_Controller_old_wiring.HEIC)

Original Fireball documentation diagrams
(Screenshot 2025-12-04 at 7.37.54 PM.png,
Screenshot 2025-12-04 at 7.38.45 PM.png)

And Current Wiring Diagram shows the current wiring setup with the arduino. 
