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
1. Finds the Arduino's USB serial port on Windows, macOS, or Linux
2. Configures D8 as a digital output
3. Lets you open/close the shutter or pulse it for a specified time

>Key configuration at the top of the script:

Pin configuration (the serial port is discovered automatically):
```python
SHUTTER_PIN_NUM = 8                    # D8 on Arduino

OPEN_STATE = 0    # logic level that OPENS the shutter (LOW in this setup)
CLOSED_STATE = 1  # logic level that CLOSES the shutter
```
Port discovery uses the existing `pyserial` dependency and its
[Windows, macOS, and Linux enumeration support](https://pyserial.readthedocs.io/en/latest/tools.html#serial.tools.list_ports.comports).
When exactly one USB serial candidate is present, it is selected automatically.
Common Arduino clones using CH340, CP210x, or FTDI adapters are supported too.
If several candidates exist, or the available devices cannot be recognized,
the script displays a numbered list and asks you to choose. In a non-interactive
terminal, supply an explicit port instead. macOS `/dev/cu.*` ports are preferred
over matching `/dev/tty.*` aliases when both are reported.

Detection reads device metadata without opening ports or sending commands.
USB metadata identifies a candidate, not its installed firmware; the selected
Arduino still needs StandardFirmata. Only the selected port is opened.

If your hardware is inverted (open actually closes), swap OPEN_STATE CLOSED_STATE.

```bash
OPEN_STATE = 1    # logic level that OPENS the shutter (HIGH in this setup)
CLOSED_STATE = 0  # logic level that CLOSES the shutter
```
**Running the Script**

Activate the environment on macOS/Linux:
```bash
source .venv/bin/activate
```

On Windows PowerShell:
```powershell
.\.venv\Scripts\Activate.ps1
```

Automatically find the port:
```bash
python ufo_shutter.py
```

List detected devices without connecting to the shutter:
```bash
python ufo_shutter.py --list-ports
```

An explicit override is still available; use the device name shown by the listing:

| Platform | Example |
| --- | --- |
| Windows | `python ufo_shutter.py --port COM3` |
| macOS | `python ufo_shutter.py --port /dev/cu.usbserial-10` |
| Linux | `python ufo_shutter.py --port /dev/ttyACM0` |

The original positional form also works, for example
`python ufo_shutter.py /dev/ttyUSB0`. If no port appears, check the data USB
cable and the board's USB serial driver. If a port cannot be opened, close any
serial monitor using it and check your account's serial-device permissions.

**CLI commands**

Once running, the script prints a small command menu:

```text
o → open shutter
c → close shutter
p <ms> → RAW commanded pulse, no compensation (default)
p <ms> o → compensated pulse for a target exposure metric
sw → raw pulse sweep using the existing duration list
swo → compensated pulse sweep using the existing duration list
ra / rb / rt → select A / select B / test switching (normal mode only)
q    → quit open (relay energized)
qoff / qcoff → quit open / closed with relay selector OFF
```

`p 10` sends an open command, waits 10 ms on the host, then sends close.
It does **not** imply 10 ms of delivered light. `p 10 o` instead maps the target
through the existing inverse lookup (or existing linear fallback); this change
does not recalibrate that mapping. `p` alone defaults to 1000 ms raw. Invalid,
zero, negative, non-integer, or extra arguments are rejected without a pulse.

### Fast mode: Shutter A only (NC)

Start the controller with automatic port discovery on Windows, macOS, or Linux:

```bash
python ufo_shutter.py --fast
```

`--port` still works with `--fast`. Normal mode remains the default and retains
the existing relay-switching delays. In fast mode:

- The selector is commanded OFF (D9 HIGH), selecting A on NC. `ra`, `rb`, and
  `rt` are disabled. All exit commands, Ctrl-C, and EOF preserve selector OFF;
  `q` leaves A open and energized.
- Cold-start/reset settling is unchanged. After `READY`, the extra one-second
  manual-command delay and five-second sweep countdown are removed. Begin camera
  recording **before** issuing `sw` or `swo`.
- Every reopening waits for at least **200 ms since the last close command**.
  Time already spent closed counts toward this guard. If a pulse is requested
  while open, the script first closes and waits. The guard is outside the pulse's
  requested duration; it is not exposure compensation.
- The 200 ms guard is a provisional starting margin, **not a measured minimum or
  safety guarantee**. The reported 75–80 ms motion time does not establish the
  complete mechanical/electrical recovery time. Validate repeatability and duty
  cycle on hardware before shortening it.
- Fast sweeps use that recovery guard as their default closed gap. Increase the
  gap for well-separated calibration events. A requested sweep gap cannot bypass
  the fast-mode recovery guard.
- Manual pulse logging is initialized before `READY`; rows are written after
  each pulse closes. Sweeps have separate logs. `success` means host writes
  returned successfully, not measured shutter motion. Times are host dispatch
  timestamps, not camera synchronization or measured Arduino edges.
- A detected serial-write failure stops further commands without retries or
  reconnecting. Hardware state is then unknown and requires inspection.

Examples with a longer recovery guard or separated calibration pulses:

```bash
python ufo_shutter.py --fast --min-closed-ms 300
python ufo_shutter.py --fast --sweep-gap-s 3
```

Important limits: the A-only lock is software-enforced **after initialization**.
Arduino reset/boot, firmware pin defaults, disconnection, or loss of power can
override it; Python cannot guarantee a permanently de-energized selector. D9 is
set HIGH before D8 writes because Firmata sends these pins together in digital
port messages. Fast mode does not call `board.exit()`, but this is not a hardware
interlock or a fix for inductive transients. Host scheduling and USB also still
introduce pulse-timing jitter; fast mode removes intentional delays, not those
sources of timing uncertainty or the shutter's physical opening/closing lag.

Offline regression tests (simulated devices and clocks; no hardware actuation):

```bash
python -m unittest discover -s tests -v
```


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
