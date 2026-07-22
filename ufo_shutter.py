# ufo_shutter_pyfirmata.py

import csv
import os
import time
import sys
from datetime import datetime
from pyfirmata import Arduino, util
from serial.serialutil import SerialException

#!!!! When you install pyfrimata, in pyfirmata.py, change inspect.getargspec to inspect.getfullargspec @ line 185 !!!

# --- CONFIG ---
# Default serial port and pin; you can override from command line.
#DEFAULT_PORT = 'COM3'      # e.g. '/dev/ttyACM0' on Linux 
#DEFAULT_PORT = '/dev/cu.usbmodem101' #mac 
#DEFAULT_PORT = '/dev/tty.usbserial-110' #MAC,but I swapped for a differnt board 
DEFAULT_PORT = '/dev/cu.usbserial-10' 
SHUTTER_PIN_NUM = 8        # D8 on Arduino

SELECT_PIN_NUM  = 9                  # D9 -> Relay IN1 and IN2 (Y-split)

# Shutter logic (your working polarity)
OPEN_STATE = 0
CLOSED_STATE = 1

# Relay logic:
# Most relay modules are ACTIVE-LOW: write(0) => relays ON (LEDs ON)
RELAY_ON  = 0
RELAY_OFF = 1

SHUTTER_LOSS_MS = 37  # calibrate later; start with 37 based on your 451 fps run
# Calibrated from 451 fps fit, shutter takes between. 75-80ms for full retraction from close
CAL_A = 0.9943244546 #slope 
CAL_B = 53.67871603  # ms (since measured ≈ A*cmd - B)
MASTER_CAL_INVERSE_CSV = os.path.join(
    os.path.dirname(__file__),
    "results_flux_ring_profile_sweep",
    "rin30_rout65_bgin85_bgout115",
    "reference",
    "master_inverse_lookup.csv",
)
COMMAND_LOG_DIR = os.path.join(os.path.dirname(__file__), "command_logs")

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

_inverse_lookup_cache = None


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

def _load_inverse_lookup(path):
    table = []
    if not path or not os.path.exists(path):
        return table

    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            table.append((float(row["target_metric_ms"]), float(row["command_ms"])))
    return table


def _interp_lookup(x, table):
    if not table:
        return None

    x = float(x)
    if x <= table[0][0]:
        return table[0][1]

    for i in range(1, len(table)):
        x0, y0 = table[i - 1]
        x1, y1 = table[i]
        if x <= x1:
            if x1 == x0:
                return y1
            frac = (x - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)

    # For targets above calibrated range, clamp to last calibrated command.
    # This avoids extrapolating outside measured data.
    return table[-1][1]


# calculate commanded pulse for desired delivered metric exposure
def cmd_for_effective_ms(target_ms: float) -> int:
    """
    Convert desired delivered exposure metric (ms) -> commanded pulse (ms).
    Prefer the monotone CSV lookup when present. Fall back to the old linear fit.
    """
    global _inverse_lookup_cache

    if _inverse_lookup_cache is None:
        _inverse_lookup_cache = _load_inverse_lookup(MASTER_CAL_INVERSE_CSV)

    cmd = _interp_lookup(target_ms, _inverse_lookup_cache)
    if cmd is None:
        cmd = (target_ms + CAL_B) / CAL_A
    return max(1, int(round(cmd)))

def _pulse_shutter_raw(pin, cmd_ms: int) -> bool:
    #actuate the shutter for cmd_ms milliseconds (no compensation)
    if not open_shutter(pin):
        return False
    time.sleep(cmd_ms / 1000.0)
    return close_shutter(pin)

def pulse_shutter(pin, duration_ms: int, offset: bool = True) -> bool:
    if offset:
        cmd_ms = cmd_for_effective_ms(duration_ms)   # duration_ms treated as target effective
    else:
        cmd_ms = int(duration_ms)                    # duration_ms treated as commanded

    return _pulse_shutter_raw(pin, cmd_ms)

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

def _start_recording_log(recording_tag: str, offset: bool):
    os.makedirs(COMMAND_LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "comp" if offset else "raw"
    name = f"{recording_tag}_{mode}_{ts}.csv"
    path = os.path.join(COMMAND_LOG_DIR, name)
    f = open(path, "w", newline="")
    writer = csv.DictWriter(
        f,
        fieldnames=[
            "pulse_index",
            "requested_ms",
            "commanded_ms",
            "compensated",
            "send_time_iso",
            "send_time_epoch_s",
            "elapsed_s",
        ],
    )
    writer.writeheader()
    return path, f, writer


def sweep_pulses(shutter_pin, durations_ms, gap_s=3.0, offset=False, recording_tag="recording"):
    """
    Runs a repeatable pulse train while you record on the camera.
    Prints timestamps so you can correlate to video if needed.
    """
    t0 = time.perf_counter()
    log_path, log_f, log_w = _start_recording_log(recording_tag=recording_tag, offset=offset)
    print(f"Command log: {log_path}")

    try:
        for i, requested_ms in enumerate(durations_ms, start=1):
            # ensure closed baseline before each test pulse
            close_shutter(shutter_pin)
            time.sleep(gap_s)

            commanded_ms = cmd_for_effective_ms(requested_ms) if offset else int(requested_ms)
            send_epoch = time.time()
            send_iso = datetime.fromtimestamp(send_epoch).isoformat(timespec="milliseconds")
            elapsed = time.perf_counter() - t0

            print(
                f"[{elapsed:8.3f}s] PULSE idx={i} req={requested_ms}ms "
                f"cmd={commanded_ms}ms send={send_iso}"
            )

            log_w.writerow({
                "pulse_index": i,
                "requested_ms": int(requested_ms),
                "commanded_ms": int(commanded_ms),
                "compensated": int(bool(offset)),
                "send_time_iso": send_iso,
                "send_time_epoch_s": f"{send_epoch:.6f}",
                "elapsed_s": f"{elapsed:.6f}",
            })
            log_f.flush()

            if not _pulse_shutter_raw(shutter_pin, int(commanded_ms)):
                print("[ERROR] Pulse command failed; stopping sweep.")
                break
    finally:
        log_f.close()

    close_shutter(shutter_pin)
    print("Sweep done.")


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
    print("  p <ms>      -> pulse with compensation from master_inverse_lookup.csv")
    print("  p <ms> n    -> pulse RAW command (no compensation)")
    print("  ra          -> select Shutter A (relays OFF -> NC) [SAFE SWITCH]")
    print("  rb          -> select Shutter B (relays ON  -> NO) [SAFE SWITCH]")
    print("  rt          -> relay toggle test (A<->B) [SAFE SWITCH]")
    print("  sw          -> enter Sweep Pulse mode (predefined pulse train) for testing")
    print("  swo         -> enter Sweep Pulse mode with offset compensation for testing")
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
                raw_mode = False
                if len(parts) > 1:
                    try:
                        duration_ms = int(parts[1])
                    except ValueError:
                        print("Invalid ms; using default 1000.")

                if len(parts) > 2:
                    token = parts[2].strip().lower()
                    if token == "n":
                        raw_mode = True
                    else:
                        print("Usage: p <ms> [n]")
                        continue

                if raw_mode:
                    cmd_ms = int(duration_ms)
                    print(f"Pulsing Shutter {current} RAW for {cmd_ms} ms (no compensation)...")
                else:
                    cmd_ms = cmd_for_effective_ms(duration_ms)
                    print(
                        f"Pulsing Shutter {current} target={duration_ms} ms "
                        f"-> compensated command={cmd_ms} ms..."
                    )

                send_epoch = time.time()
                send_iso = datetime.fromtimestamp(send_epoch).isoformat(timespec="milliseconds")
                log_path, log_f, log_w = _start_recording_log(
                    recording_tag="manual_pulse",
                    offset=(not raw_mode),
                )
                log_w.writerow({
                    "pulse_index": 1,
                    "requested_ms": int(duration_ms),
                    "commanded_ms": int(cmd_ms),
                    "compensated": int(not raw_mode),
                    "send_time_iso": send_iso,
                    "send_time_epoch_s": f"{send_epoch:.6f}",
                    "elapsed_s": "0.000000",
                })
                log_f.close()
                print(f"Command log: {log_path}")

                if not _pulse_shutter_raw(shutter_pin, cmd_ms):
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

            elif cmd == 'sw':
                # sweep for timing characterization in milliseconds
                durations1 = [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]
                durations2 = [10,12,15,17,20,22,24,26,28,30,32,34,36,38,40,42,45,50,60,75,80,85,100,150]
                durations3 = [10,11,12,13,14,15,16,17,18,19,20,22,24,26,28,30]
                durations4 = [75,76,77,78,79,80,81,82,83,84,85]
                gap_s = 3.0 # gap between pulses in seconds 
                print("Starting sweep. Start ASICap recording now.")
                time.sleep(5.0)
                sweep_pulses(
                    shutter_pin,
                    durations_ms=durations4,
                    gap_s=gap_s,
                    offset=False,
                    recording_tag="sw_durations4",
                )

            elif cmd == 'swo':
                # sweep for timing characterization in milliseconds
                durations = [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]
                gap_s = 2.0 # gap between pulses in seconds
                print("Starting sweep with offset compensation. Start ASICap recording now.")
                time.sleep(5.0)
                sweep_pulses(
                    shutter_pin,
                    durations_ms=durations,
                    gap_s=gap_s,
                    offset=True,
                    recording_tag="swo_durations1",
                )

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
                print("Unknown command. Use: o, c, p <ms>, p <ms> n, ra, rb, rt, q")

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
