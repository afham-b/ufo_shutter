# ufo_shutter_pyfirmata.py

import time
import sys
from pyfirmata import Arduino, util

#!!!! When you install pyfrimata, in pyfirmata.py, change inspect.getargspec to inspect.getfullargspec @ line 185 !!!

# --- CONFIG ---
# Default serial port and pin; you can override from command line.
#DEFAULT_PORT = 'COM3'      # e.g. '/dev/ttyACM0' on Linux 
DEFAULT_PORT = '/dev/cu.usbmodem101' #mac 
SHUTTER_PIN_NUM = 8        # D8 on Arduino

# Logic levels: if this is backwards for your hardware, swap 1 and 0
OPEN_STATE = 0   # HIGH
CLOSED_STATE = 1 # LOW


def main(port=DEFAULT_PORT, pin_num=SHUTTER_PIN_NUM):
    print(f"Connecting to Arduino on {port}...")
    board = Arduino(port)

    # Get digital pin D8 as an output: 'd:<pin>:o'
    shutter_pin = board.get_pin(f'd:{pin_num}:o')

    # Make sure shutter starts closed
    close_shutter(shutter_pin)

    print("Connected.")
    print("Commands:")
    print("  o           -> open shutter")
    print("  c           -> close shutter")
    print("  p <ms>      -> pulse open for <ms> milliseconds (e.g. 'p 500')")
    print("  q           -> quit")

    try:
        while True:
            cmd_line = input("> ").strip()
            if not cmd_line:
                continue

            parts = cmd_line.split()
            cmd = parts[0].lower()

            if cmd == 'o':
                open_shutter(shutter_pin)
                print("Shutter: OPEN")

            elif cmd == 'c':
                close_shutter(shutter_pin)
                print("Shutter: CLOSED")

            elif cmd == 'p':
                # default 1000 ms if not provided
                duration_ms = 1000
                if len(parts) > 1:
                    try:
                        duration_ms = int(parts[1])
                    except ValueError:
                        print("Invalid ms; using default 1000.")
                print(f"Pulsing open for {duration_ms} ms...")
                pulse_shutter(shutter_pin, duration_ms)

            elif cmd == 'q':
                print("Quitting.")
                break

            else:
                print("Unknown command. Use: o, c, p <ms>, q")

    finally:
        print("Closing shutter and releasing board...")
        close_shutter(shutter_pin)
        board.exit()
        print("Done.")


def open_shutter(pin):
    pin.write(OPEN_STATE)


def close_shutter(pin):
    pin.write(CLOSED_STATE)


def pulse_shutter(pin, duration_ms):
    open_shutter(pin)
    time.sleep(duration_ms / 1000.0)
    close_shutter(pin)


if __name__ == "__main__":
    # Optional: allow port override via CLI: python ufo_shutter_pyfirmata.py COM5
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        port = sys.argv[1]
    main(port=port)
