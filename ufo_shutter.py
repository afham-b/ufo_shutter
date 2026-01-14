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
PRE_SWITCH_OPEN_SEC   = 0.5       # open + let V880/coil settle before switching
PRE_SWITCH_CLOSE_SEC   = 2.0      # close + let V880/coil settle before switching
POST_SWITCH_SETTLE_SEC = 5.0      # let relay contacts settle after switching
SECOND_CLOSE_AFTER_SWITCH = True  # helps ensure new shutter is in a known state
DELAY_BEFORE_COMMAND = 1.0      # wait time after selecting shutter before sending commands

#Params on how to exit script, and which state to leave shutter in
EXIT_MODE = "open"   # "open" or "closed" # shutter state on exit, denergized is "open"
EXIT_RELAY = "on"           # relay state on exit: "on" (energized LEDs on) or "off" Sometime the relays reset on exit so we leave it on. 
DO_BOARD_EXIT = False  # if False, we DON'T call board.exit() so pins stay latched more reliably


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


def open_shutter(pin, delay=False) -> bool:
    if delay:
        time.sleep(DELAY_BEFORE_COMMAND)
    return safe_write(pin, OPEN_STATE)

def close_shutter(pin, delay=False) -> bool:
    if delay:
        time.sleep(DELAY_BEFORE_COMMAND)
    return safe_write(pin, CLOSED_STATE)

def pulse_shutter(pin, duration_ms: int) -> bool:
    if not open_shutter(pin):
        return False
    time.sleep(duration_ms / 1000.0)
    return close_shutter(pin)

def relay_on(sel_pin) -> bool:
    return safe_write(sel_pin, RELAY_ON)

def relay_off(sel_pin) -> bool:
    return safe_write(sel_pin, RELAY_OFF)

def safe_select(target: str, sel_pin, shutter_pin) -> bool:
    """
    Safe relay switch:
      1) open shutter (avoid switching while energized, which is the closed state)
      2) wait for coil/driver to settle
      3) switch the relay
      4) wait for contacts to settles 
      5) optional: close again (new shutter known state)
    """
    # if not close_shutter(shutter_pin):
    #     return False
    # time.sleep(PRE_SWITCH_CLOSE_SEC)

    # open the shutter to de-energize the coil 
    if not open_shutter(shutter_pin):
        return False
    time.sleep(PRE_SWITCH_OPEN_SEC)

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

    global EXIT_MODE, EXIT_RELAY

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
    
    print("Seting up, please wait ~10 seconds.")
    time.sleep(5.0)
    select_shutter_a(sel_pin)
    current = "A"
    time.sleep(3.0)
    close_shutter(shutter_pin)
    time.sleep(2.0)

     # Command loop
    print("Connected. Safe Switching version")
    print("Commands:")
    print("  o           -> open shutter (selected)")
    print("  c           -> close shutter")
    print("  p <ms>      -> pulse open for <ms> milliseconds (e.g. 'p 500')")
    print("  ra          -> select Shutter A (relays OFF -> NC) [SAFE SWITCH]")
    print("  rb          -> select Shutter B (relays ON  -> NO) [SAFE SWITCH]")
    print("  rt          -> relay toggle test (A<->B) [SAFE SWITCH]")
    print("  q           -> quit")
    #print("  qc          -> quit, leaving selected shutter CLOSED (energized)")
    print(f"\nCurrent shutter: {current}")

    try:
        while True:
            cmd_line = input("> ").strip()
            if not cmd_line:
                continue

            parts = cmd_line.split()
            cmd = parts[0].lower()

            if cmd == 'o':
                if not open_shutter(shutter_pin, delay=True):
                    break
                print(f"Shutter {current}: OPEN")

            elif cmd == 'c':
                if not close_shutter(shutter_pin, delay=True):
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
                print("Switching to Shutter B...please wait 10 seconds for safety before sending commands.")
                if not safe_select("A", sel_pin, shutter_pin):
                    break
                current = "A"
                print("Selected Shutter A (relays OFF -> NC)")

            elif cmd == 'rb':
                print("Switching to Shutter B...please wait 10 seconds for safety before sending commands.")
                if not safe_select("B", sel_pin, shutter_pin):
                    break
                current = "B"
                time.sleep(5.0)
                print("Selected Shutter B (relays ON -> NO)")

                # #testing states 
                # #close_shutter(shutter_pin)  # ensure closed after switch
                # open_shutter(shutter_pin)   # optional: open after switch
                # close_shutter(shutter_pin)  # ensure closed after test

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
                # default quit behavior
                EXIT_MODE = "open"   # change default if you want
                EXIT_RELAY = "on"
                print("Quitting (default: leave shutter OPEN) and RELAY ON. Use qc for forced closed exit. May require manual reset.")
                break
                
            elif cmd == 'qc':
                EXIT_MODE = "closed"
                EXIT_RELAY = "on"
                print("Quitting: leave shutter CLOSED (energized) and RELAY ON.")
                break

            elif cmd == 'qoff':
                EXIT_MODE = "open"
                EXIT_RELAY = "off"
                print("Quitting: leave shutter OPEN and relay OFF.")
                break

            elif cmd == 'qcoff':
                EXIT_MODE = "closed"
                EXIT_RELAY = "off"
                print("Quitting: leave shutter OPEN and relay OFF.")
                break


            else:
                print("Unknown command. Use: o, c, p <ms>, ra, rb, rt, q")

    finally:
        print("Exiting...setting final states...")

        #1) Shutter exit state (skip the 1s command delay on exit)
        try:
            if EXIT_MODE == "open":
                open_shutter(shutter_pin, delay=False)
                print("Exit mode: Shutter OPEN (de-energized).")
            else:
                close_shutter(shutter_pin, delay=False)
                print("Exit mode: Shutter CLOSED.")
        except Exception:
            pass

        #2) Relay exit state
        try:
            if EXIT_RELAY == "on":
                relay_on(sel_pin)   # RELAY_ON (active-low -> write(0)) => LEDs on
                print("Exit mode: Relay ON (energized, LEDs on).")
            else:
                relay_off(sel_pin)
                print("Exit mode: Relay OFF.")
        except Exception:
            pass

        # Give hardware a moment to settle before ending the program
        time.sleep(0.5)

        # IMPORTANT:
        # If you call board.exit(), pyFirmata shuts down comms and some boards may reset pins.
        # If you want the pin to remain latched, try leaving DO_BOARD_EXIT=False.
        if DO_BOARD_EXIT:
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