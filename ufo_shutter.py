# ufo_shutter_pyfirmata.py

import argparse
import csv
import math
import os
import time
import sys
from datetime import datetime
from serial.serialutil import SerialException
from serial_ports import (
    PortSelectionError,
    available_ports,
    describe_port,
    select_serial_port,
)

#!!!! When you install pyfrimata, in pyfirmata.py, change inspect.getargspec to inspect.getfullargspec @ line 185 !!!

# --- CONFIG ---
# The serial port is auto-detected unless supplied on the command line.
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
FAST_MIN_CLOSED_MS = 200.0     # provisional recovery guard, not a measured minimum

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


class RelayOffPin:
    """In fast mode, allow only the NC/A selector state after initialization."""

    def __init__(self, pin):
        self.pin = pin
        self.failed = False

    def write(self, value):
        if value != RELAY_OFF:
            raise ValueError("Fast mode locks the selector OFF on Shutter A.")
        if self.failed:
            raise SerialException("Selector connection previously failed; restart required.")
        try:
            self.pin.write(value)
        except (OSError, SerialException):
            self.failed = True
            raise


class FastShutterPin:
    """Track commanded state and wait only for outstanding closure recovery.

    This is a host-side time guard, not feedback of the physical blade position.
    A serial failure is latched because Firmata may have cached an unsent value.
    """

    def __init__(self, pin, min_closed_ms=FAST_MIN_CLOSED_MS):
        if not math.isfinite(min_closed_ms) or min_closed_ms <= 0:
            raise ValueError("Minimum closed time must be finite and greater than zero.")
        self.pin = pin
        self.min_closed_s = min_closed_ms / 1000.0
        self.state = None
        self.closed_at = None
        self.failed = False

    def wait_closed(self, minimum_s=0.0):
        if self.closed_at is not None:
            remaining = max(self.min_closed_s, minimum_s) - (time.monotonic() - self.closed_at)
            if remaining > 0:
                time.sleep(remaining)

    def write(self, value):
        if self.failed:
            raise SerialException("Shutter connection previously failed; restart required.")
        if value == OPEN_STATE and self.state == CLOSED_STATE:
            self.wait_closed()
        try:
            self.pin.write(value)
        except (OSError, SerialException):
            self.failed = True
            self.state = None
            self.closed_at = None
            raise
        if value == CLOSED_STATE and self.state != CLOSED_STATE:
            self.closed_at = time.monotonic()
        elif value == OPEN_STATE:
            self.closed_at = None
        self.state = value

    def prepare_pulse(self, minimum_s=0.0):
        # An open or unknown starting state must first be closed and allowed
        # to recover. Repeated close commands do not restart the recovery clock.
        if not safe_write(self, CLOSED_STATE):
            return False
        self.wait_closed(minimum_s)
        return True


def parse_pulse_command(parts):
    """p [ms] is raw; p <ms> o opts in to compensation; n remains a raw alias."""
    if len(parts) > 3 or (len(parts) == 3 and parts[2].lower() not in ("o", "n")):
        raise ValueError("Usage: p <positive integer ms> [o|n]")
    try:
        duration_ms = int(parts[1]) if len(parts) > 1 else 1000
    except ValueError:
        raise ValueError("Pulse duration must be a positive integer in milliseconds.") from None
    if duration_ms <= 0:
        raise ValueError("Pulse duration must be a positive integer in milliseconds.")
    offset = len(parts) == 3 and parts[2].lower() == "o"
    return duration_ms, offset


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
    if not isinstance(cmd_ms, int) or isinstance(cmd_ms, bool) or cmd_ms <= 0:
        raise ValueError("Pulse duration must be a positive integer in milliseconds.")
    if isinstance(pin, FastShutterPin) and not pin.prepare_pulse():
        return False
    if not open_shutter(pin):
        return False
    try:
        time.sleep(cmd_ms / 1000.0)
    finally:
        closed = close_shutter(pin)
    return closed

def pulse_shutter(pin, duration_ms: int, offset: bool = False) -> bool:
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms <= 0:
        raise ValueError("Pulse duration must be a positive integer in milliseconds.")
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

def _start_recording_log(recording_tag: str, offset):
    os.makedirs(COMMAND_LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mode = "mixed" if offset is None else ("comp" if offset else "raw")
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
            "success",
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
            if isinstance(shutter_pin, FastShutterPin):
                if not shutter_pin.prepare_pulse(minimum_s=gap_s):
                    return False
            else:
                if not close_shutter(shutter_pin):
                    return False
                time.sleep(gap_s)

            commanded_ms = cmd_for_effective_ms(requested_ms) if offset else int(requested_ms)
            print(
                f"PULSE idx={i} req={requested_ms}ms cmd={commanded_ms}ms"
            )
            send_epoch = time.time()
            elapsed = time.perf_counter() - t0
            succeeded = _pulse_shutter_raw(shutter_pin, int(commanded_ms))
            # Disk I/O is outside the open/close pulse interval. Timestamps are
            # host dispatch times, not measured electrical edges at the Arduino.
            log_w.writerow({
                "pulse_index": i,
                "requested_ms": int(requested_ms),
                "commanded_ms": int(commanded_ms),
                "compensated": int(bool(offset)),
                "send_time_iso": datetime.fromtimestamp(send_epoch).isoformat(timespec="milliseconds"),
                "send_time_epoch_s": f"{send_epoch:.6f}",
                "elapsed_s": f"{elapsed:.6f}",
                "success": int(succeeded),
            })
            log_f.flush()

            if not succeeded:
                print("[ERROR] Pulse command failed; stopping sweep.")
                return False
    finally:
        log_f.close()

    if not close_shutter(shutter_pin):
        return False
    print("Sweep done.")
    return True


def main(port=None, fast=False, min_closed_ms=FAST_MIN_CLOSED_MS, sweep_gap_s=None):
    if not math.isfinite(min_closed_ms) or min_closed_ms <= 0:
        raise ValueError("Minimum closed time must be finite and greater than zero.")
    if sweep_gap_s is not None and (not math.isfinite(sweep_gap_s) or sweep_gap_s <= 0):
        raise ValueError("Sweep gap must be finite and greater than zero.")
    exit_mode = EXIT_MODE
    exit_relay = "off" if fast else EXIT_RELAY
    session_log = None
    pulse_index = 0

    port = select_serial_port(port)
    # Keep discovery and --list-ports independent of Firmata connections.
    from pyfirmata import Arduino, util

    if fast:
        print("FAST mode: the A-only lock applies after initialization; firmware controls relay state during reset/boot.")
    print(f"Connecting to Arduino on {port}...")
    try:
        board = Arduino(port)
    except (OSError, SerialException) as exc:
        raise PortSelectionError(
            f"Could not open {port}: {exc}. Check the USB connection, close other "
            "programs using the port, and check your serial-device permissions. "
            "Use --list-ports to inspect devices or --port to select another."
        ) from exc

    # Start Firmata iterator thread (improves robustness)
    it = util.Iterator(board)
    it.start()

    # Arduino usually resets on connect
    time.sleep(2.0)
    print("Setting up pins...please wait...")

    shutter_pin = board.get_pin(f'd:{SHUTTER_PIN_NUM}:o')  # D8 output
    sel_pin     = board.get_pin(f'd:{SELECT_PIN_NUM}:o')   # D9 output
    if fast:
        shutter_pin = FastShutterPin(shutter_pin, min_closed_ms=min_closed_ms)
        sel_pin = RelayOffPin(sel_pin)

    # Safe startup
    
    print("Setting up, please wait ~10 seconds. Startup settling is retained.")
    time.sleep(5.0)
    # D8 and D9 share a Firmata digital port. Establish D9 HIGH before any
    # D8 writes so those combined port messages also keep the relay OFF.
    if not select_shutter_a(sel_pin):
        raise PortSelectionError("Could not establish selector OFF/A; startup aborted. Hardware state is unknown.")
    current = "A"
    time.sleep(3.0)
    if not close_shutter(shutter_pin):
        raise PortSelectionError("Could not close Shutter A; startup aborted. Hardware state is unknown.")
    # Last successfully commanded state, not physical blade-position feedback.
    commanded_state = CLOSED_STATE
    time.sleep(2.0)

     # Command loop
    print("Connected. FAST A-only mode" if fast else "Connected. Safe Switching version")
    if fast:
        print(f"Selector locked OFF/NC. Minimum commanded-closed recovery: {min_closed_ms:g} ms (provisional).")
        print("Start camera recording BEFORE issuing sw/swo; fast sweeps have no countdown.")
    print("Commands:")
    print("  o           -> open shutter (selected)")
    print("  c           -> close shutter")
    print("  p <ms>      -> RAW pulse (no compensation; default)")
    print("  p <ms> o    -> offset/compensated pulse using the existing calibration")
    if fast:
        print("  ra/rb/rt    -> disabled; Shutter A only")
    else:
        print("  ra          -> select Shutter A (relays OFF -> NC) [SAFE SWITCH]")
        print("  rb          -> select Shutter B (relays ON  -> NO) [SAFE SWITCH]")
        print("  rt          -> relay toggle test (A<->B) [SAFE SWITCH]")
    print("  sw          -> enter Sweep Pulse mode (predefined pulse train) for testing")
    print("  swo         -> enter Sweep Pulse mode with offset compensation for testing")
    print("  q           -> quit OPEN and relay energized")
    print("  qoff/qcoff  -> quit OPEN/CLOSED with RELAY selector OFF (always OFF in fast mode)")
    print(f"\nCurrent shutter: {current}")

    try:
        if fast:
            # Allocate the log before READY, not in the first pulse's path.
            log_path, log_f, log_w = _start_recording_log("fast_manual", offset=None)
            session_log = (log_f, log_w)
            print(f"Manual command log: {log_path}")
        session_t0 = time.perf_counter()
        print("READY")
        while True:
            cmd_line = input("> ").strip()
            if not cmd_line:
                continue

            parts = cmd_line.split()
            cmd = parts[0].lower()

            if fast and cmd in ('ra', 'rb', 'rt'):
                print("Fast mode is locked to Shutter A (NC); relay switching is disabled.")
                continue

            if cmd == 'o':
                if not open_shutter(shutter_pin, delay=not fast):
                    break
                commanded_state = OPEN_STATE
                print(f"Shutter {current}: OPEN")

            elif cmd == 'c':
                if not close_shutter(shutter_pin, delay=not fast):
                    break
                commanded_state = CLOSED_STATE
                print(f"Shutter {current}: CLOSED")

            elif cmd == 'p':
                try:
                    duration_ms, offset = parse_pulse_command(parts)
                except ValueError as exc:
                    print(exc)
                    continue
                cmd_ms = cmd_for_effective_ms(duration_ms) if offset else duration_ms
                if commanded_state == OPEN_STATE:
                    print(
                        f"[WARNING] Shutter {current} was already commanded OPEN before this manual pulse. "
                        "Your recording may include extra exposure before the pulse; "
                        "the requested duration/compensation does not account for that light."
                    )
                    if fast:
                        print(
                            f"Fast mode: close -> wait at least {min_closed_ms:g} ms -> "
                            f"open for {cmd_ms} ms -> close. "
                            "Closing first cannot remove light already recorded."
                        )
                    else:
                        print(
                            f"Normal mode: continue the existing open interval for {cmd_ms} ms, "
                            "then close. This is not an isolated exposure."
                        )
                if fast and not shutter_pin.prepare_pulse():
                    break
                send_epoch = time.time()
                elapsed = time.perf_counter() - session_t0
                succeeded = _pulse_shutter_raw(shutter_pin, cmd_ms)
                commanded_state = CLOSED_STATE if succeeded else None
                pulse_index += 1
                row = {
                    "pulse_index": pulse_index if fast else 1,
                    "requested_ms": int(duration_ms),
                    "commanded_ms": int(cmd_ms),
                    "compensated": int(offset),
                    "send_time_iso": datetime.fromtimestamp(send_epoch).isoformat(timespec="milliseconds"),
                    "send_time_epoch_s": f"{send_epoch:.6f}",
                    "elapsed_s": f"{elapsed:.6f}" if fast else "0.000000",
                    "success": int(succeeded),
                }
                if fast:
                    log_f, log_w = session_log
                    log_w.writerow(row)
                    log_f.flush()
                else:
                    log_path, log_f, log_w = _start_recording_log("manual_pulse", offset=offset)
                    try:
                        log_w.writerow(row)
                    finally:
                        log_f.close()
                    print(f"Command log: {log_path}")
                if not succeeded:
                    print("[ERROR] Pulse command failed; stopping.")
                    break
                mode = "COMPENSATED" if offset else "RAW"
                print(f"Shutter {current}: {mode} request={duration_ms} ms, command={cmd_ms} ms; close command sent.")

            elif cmd == 'ra':
                print("Switching to Shutter A...please wait 10 seconds for safety before sending commands.")
                if not safe_select("A", sel_pin, shutter_pin):
                    break
                current = "A"
                commanded_state = CLOSED_STATE if SECOND_CLOSE_AFTER_SWITCH else OPEN_STATE
                print("Selected Shutter A (relays OFF -> NC)")

            elif cmd == 'rb':
                print("Switching to Shutter B...please wait 10 seconds for safety before sending commands.")
                if not safe_select("B", sel_pin, shutter_pin):
                    break
                current = "B"
                commanded_state = CLOSED_STATE if SECOND_CLOSE_AFTER_SWITCH else OPEN_STATE
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
                commanded_state = CLOSED_STATE if SECOND_CLOSE_AFTER_SWITCH else OPEN_STATE
                print("Relay test done.")

            elif cmd == 'sw':
                # sweep for timing characterization in milliseconds
                durations1 = [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]
                durations2 = [10,12,15,17,20,22,24,26,28,30,32,34,36,38,40,42,45,50,60,75,80,85,100,150]
                durations3 = [10,11,12,13,14,15,16,17,18,19,20,22,24,26,28,30]
                durations4 = [75,76,77,78,79,80,81,82,83,84,85]
                gap_s = sweep_gap_s if sweep_gap_s is not None else (min_closed_ms / 1000.0 if fast else 3.0)
                print("Starting RAW sweep." if fast else "Starting sweep. Start ASICap recording now.")
                if not fast:
                    time.sleep(5.0)
                if not sweep_pulses(
                    shutter_pin,
                    durations_ms=durations4,
                    gap_s=gap_s,
                    offset=False,
                    recording_tag="sw_durations4",
                ):
                    break
                commanded_state = CLOSED_STATE

            elif cmd == 'swo':
                # sweep for timing characterization in milliseconds
                durations = [10,20,30,50,75,100,150,200,250,260,270,280,287,290,300,310,500,750,1000,1500,2000,2500,3000,4000]
                gap_s = sweep_gap_s if sweep_gap_s is not None else (min_closed_ms / 1000.0 if fast else 2.0)
                print("Starting compensated sweep." if fast else "Starting sweep with offset compensation. Start ASICap recording now.")
                if not fast:
                    time.sleep(5.0)
                if not sweep_pulses(
                    shutter_pin,
                    durations_ms=durations,
                    gap_s=gap_s,
                    offset=True,
                    recording_tag="swo_durations1",
                ):
                    break
                commanded_state = CLOSED_STATE

            elif cmd in ('q', 'qc', 'qoff', 'qcoff'):
                exit_mode = "closed" if cmd in ('qc', 'qcoff') else "open"
                exit_relay = "off" if fast or cmd in ('qoff', 'qcoff') else "on"
                print(f"Quitting: leave shutter {exit_mode.upper()} and relay {exit_relay.upper()}.")
                break


            else:
                print("Unknown command. Use: o, c, p <ms>, p <ms> o, sw, swo, q, qc")

    except (KeyboardInterrupt, EOFError):
        print("\nStopping; applying configured exit states.")

    finally:
        print("Exiting...setting final states...")

        if fast and (shutter_pin.failed or sel_pin.failed):
            print("[ERROR] Serial failure: hardware state is unknown. No retries or relay changes; inspect/reset manually.")
        else:
            # Fast exits never energize the selector, including q/qc and Ctrl-C.
            action = open_shutter if exit_mode == "open" else close_shutter
            if action(shutter_pin, delay=False):
                print(f"Exit command sent: shutter {exit_mode.upper()}.")
            else:
                print("[ERROR] Could not set shutter exit state; hardware state is unknown.")
            if not fast or not shutter_pin.failed:
                relay_action = relay_off if fast or exit_relay == "off" else relay_on
                if relay_action(sel_pin):
                    print(f"Exit command sent: relay {'OFF' if fast else exit_relay.upper()}.")
                else:
                    print("[ERROR] Could not set selector exit state; hardware state is unknown.")

        if session_log is not None:
            session_log[0].close()

        # Give hardware a moment to settle before ending the program
        time.sleep(0.5)

        # IMPORTANT:
        # If you call board.exit(), pyFirmata shuts down comms and some boards may reset pins.
        # If you want the pin to remain latched, try leaving DO_BOARD_EXIT=False.
        if DO_BOARD_EXIT and not fast:
            try:
                board.exit()
            except Exception:
                pass

        print("Done.")

    return not (fast and (shutter_pin.failed or sel_pin.failed))


def cli(argv=None):
    def positive_float(value):
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a positive finite number") from None
        if not math.isfinite(number) or number <= 0:
            raise argparse.ArgumentTypeError("must be a positive finite number")
        return number

    parser = argparse.ArgumentParser(
        description="Control the UFO shutter; auto-detect the Arduino serial port."
    )
    parser.add_argument("port", nargs="?", help="Explicit port (legacy positional form)")
    parser.add_argument("--port", dest="port_override", help="Explicit serial port")
    parser.add_argument(
        "--list-ports", action="store_true", help="List serial ports without connecting"
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Lock to Shutter A/NC, disable switching, and remove runtime command/countdown delays",
    )
    parser.add_argument(
        "--min-closed-ms", type=positive_float, default=FAST_MIN_CLOSED_MS,
        help=f"Fast-mode closed recovery guard in ms (default: {FAST_MIN_CLOSED_MS:g}; provisional)",
    )
    parser.add_argument(
        "--sweep-gap-s", type=positive_float,
        help="Closed gap for sw/swo; fast mode also enforces --min-closed-ms",
    )
    args = parser.parse_args(argv)
    if args.port is not None and args.port_override is not None:
        parser.error("Specify either a positional port or --port, not both.")

    try:
        if args.list_ports:
            ports = available_ports()
            for port in ports:
                print(describe_port(port))
            if not ports:
                print("No serial ports found.")
            return 0
        succeeded = main(
            port=args.port_override if args.port_override is not None else args.port,
            fast=args.fast,
            min_closed_ms=args.min_closed_ms,
            sweep_gap_s=args.sweep_gap_s,
        )
        if succeeded is False:
            return 1
    except PortSelectionError as exc:
        print(f"[PORT ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
