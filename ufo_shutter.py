# ufo_shutter_pyfirmata.py

import time
import sys
from pyfirmata import Arduino, util
from serial.serialutil import SerialException

#!!!! When you install pyfrimata, in pyfirmata.py, change inspect.getargspec to inspect.getfullargspec @ line 185 !!!

# --- CONFIG ---
# Default serial port and pin; you can override from command line.
#DEFAULT_PORT = 'COM3'      # e.g. '/dev/ttyACM0' on Linux 
DEFAULT_PORT = '/dev/cu.usbmodem101' #mac 
SHUTTER_PIN_NUM = 8        # D8 on Arduino

SELECT_PIN_NUM  = 9                  # D9 -> Relay IN1 and IN2 (Y-split)

# Shutter logic (your working polarity)
OPEN_STATE = 0
CLOSED_STATE = 1

# Relay logic:
# Most relay modules are ACTIVE-LOW: write(0) => relays ON (LEDs ON)
RELAY_ON  = 0
RELAY_OFF = 1


#Switching guards (tune these as needed)
PRE_SWITCH_CLOSE_SEC   = 2.0      # close + let V880/coil settle before switching
POST_SWITCH_SETTLE_SEC = 5.0      # let relay contacts settle after switching
SECOND_CLOSE_AFTER_SWITCH = True  # helps ensure new shutter is in a known state


#for serial crashes that can happen if arduino is overvolted by back EMI from relays
def safe_write(pin, value) -> bool:
    """Write to a Firmata pin but don't crash if USB/serial drops."""
    try:
        pin.write(value)
        return True
    except (OSError, SerialException) as e:
        print(f"[SERIAL LOST] {e}")
        return False

# Wiring assumption:
# Shutter A on NC (default when relays OFF)  -> COM->NC
# Shutter B on NO (when relays ON)           -> COM->NO

def select_shutter_a(sel_pin) -> bool:
    return safe_write(sel_pin, RELAY_OFF)  # relays OFF => NC => Shutter A

def select_shutter_b(sel_pin) -> bool:
    return safe_write(sel_pin, RELAY_ON)   # relays ON  => NO => Shutter B


def open_shutter(pin) -> bool:
    return safe_write(pin, OPEN_STATE)

def close_shutter(pin) -> bool:
    return safe_write(pin, CLOSED_STATE)


def pulse_shutter(pin, duration_ms: int) -> bool:
    if not open_shutter(pin):
        return False
    time.sleep(duration_ms / 1000.0)
    return close_shutter(pin)

def safe_select(target: str, sel_pin, shutter_pin) -> bool:
    """
    Safe relay switch:
      1) close shutter (avoid switching while energized)
      2) wait for coil/driver to settle
      3) switch the relay
      4) wait for contacts to settles 
      5) optional: close again (new shutter known state)
    """
    if not close_shutter(shutter_pin):
        return False
    time.sleep(PRE_SWITCH_CLOSE_SEC)

    if target.upper() == "A":
        ok = select_shutter_a(sel_pin)
    else:
        ok = select_shutter_b(sel_pin)

    if not ok:
        return False

    time.sleep(POST_SWITCH_SETTLE_SEC)

    if SECOND_CLOSE_AFTER_SWITCH:
        if not close_shutter(shutter_pin):
            return False
        time.sleep(0.1)

    return True


def main(port=DEFAULT_PORT):
    print(f"Connecting to Arduino on {port}...")
    board = Arduino(port)

    # Start Firmata iterator thread (improves robustness)
    it = util.Iterator(board)
    it.start()

    # Arduino usually resets on connect
    time.sleep(2.0)
    print("Setting up pins...please wait...")

    shutter_pin = board.get_pin(f'd:{SHUTTER_PIN_NUM}:o')  # D8 output
    sel_pin     = board.get_pin(f'd:{SELECT_PIN_NUM}:o')   # D9 output

    # Safe startup
    close_shutter(shutter_pin)
    select_shutter_a(sel_pin)
    current = "A"
    print("Seting up, please wait 5 seconds.")
    time.sleep(5.0)

    print("Connected. Safe Switching version")
    print("Commands:")
    print("  o           -> open shutter (selected)")
    print("  c           -> close shutter")
    print("  p <ms>      -> pulse open for <ms> milliseconds (e.g. 'p 500')")
    print("  ra          -> select Shutter A (relays OFF -> NC) [SAFE SWITCH]")
    print("  rb          -> select Shutter B (relays ON  -> NO) [SAFE SWITCH]")
    print("  rt          -> relay toggle test (A<->B) [SAFE SWITCH]")
    print("  q           -> quit")
    print(f"\nCurrent shutter: {current}")

    try:
        while True:
            cmd_line = input("> ").strip()
            if not cmd_line:
                continue

            parts = cmd_line.split()
            cmd = parts[0].lower()

            if cmd == 'o':
                if not open_shutter(shutter_pin):
                    break
                print(f"Shutter {current}: OPEN")

            elif cmd == 'c':
                if not close_shutter(shutter_pin):
                    break
                print(f"Shutter {current}: CLOSED")

            elif cmd == 'p':
                duration_ms = 1000
                if len(parts) > 1:
                    try:
                        duration_ms = int(parts[1])
                    except ValueError:
                        print("Invalid ms; using default 1000.")
                print(f"Pulsing Shutter {current} open for {duration_ms} ms...")
                if not pulse_shutter(shutter_pin, duration_ms):
                    break

            elif cmd == 'ra':
                if not safe_select("A", sel_pin, shutter_pin):
                    break
                current = "A"
                print("Selected Shutter A (relays OFF -> NC)")

            elif cmd == 'rb':
                print("Switching to Shutter B...please wait 10 seconds for safety.")
                if not safe_select("B", sel_pin, shutter_pin):
                    break
                current = "B"
                print("Selected Shutter B (relays ON -> NO)")

                #testing states 
                #close_shutter(shutter_pin)  # ensure closed after switch
                open_shutter(shutter_pin)   # optional: open after switch
                close_shutter(shutter_pin)  # ensure closed after test

            elif cmd == 'rt':
                print("Relay toggle test (SAFE): A -> B -> A -> B -> A")
                for _ in range(2):
                    if not safe_select("A", sel_pin, shutter_pin): break
                    print("  A (OFF)"); time.sleep(0.4)
                    if not safe_select("B", sel_pin, shutter_pin): break
                    print("  B (ON)"); time.sleep(0.4)
                if not safe_select("A", sel_pin, shutter_pin):
                    break
                print("  A (OFF)")
                current = "A"
                print("Relay test done.")

            elif cmd == 'q':
                print("Quitting.")
                break

            else:
                print("Unknown command. Use: o, c, p <ms>, ra, rb, rt, q")

    finally:
        print("Closing shutter and releasing board...")
        try:
            close_shutter(shutter_pin)
        except Exception:
            pass
        try:
            select_shutter_a(sel_pin)
        except Exception:
            pass
        try:
            board.exit()
        except Exception:
            pass
        print("Done.")


if __name__ == "__main__":
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        port = sys.argv[1]
    main(port=port)