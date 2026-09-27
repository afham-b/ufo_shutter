"""Find the shutter's serial port without opening or probing any devices."""

import sys

from serial.tools import list_ports


class PortSelectionError(RuntimeError):
    """No port could be selected or opened for the controller."""


def available_ports():
    """Enumerate Windows/macOS/Linux ports, deduplicating macOS tty/cu pairs."""
    try:
        ports = list_ports.comports()
    except OSError as exc:
        raise PortSelectionError(f"Could not enumerate serial ports: {exc}") from exc

    unique = {}
    for port in ports:
        # Only prefer a /dev/cu.* alias if it was actually enumerated.
        key = port.device
        if key.startswith("/dev/tty."):
            key = "/dev/cu." + key[len("/dev/tty."):]
        if key not in unique or port.device.startswith("/dev/cu."):
            unique[key] = port
    # ListPortInfo implements natural ordering (COM2 before COM10).
    return sorted(unique.values())


def describe_port(port):
    details = [port.description or "Unknown serial device"]
    if port.vid is not None and port.pid is not None:
        details.append(f"USB {port.vid:04X}:{port.pid:04X}")
    if port.serial_number:
        details.append(f"serial={port.serial_number}")
    return f"{port.device} — {'; '.join(details)}"


def is_controller_candidate(port):
    """Recognize USB/Arduino serial devices; this does not verify Firmata."""
    metadata = " ".join(
        str(getattr(port, field, None) or "")
        for field in ("device", "description", "manufacturer", "product", "hwid")
    ).lower()
    if "bluetooth" in metadata or "bthenum" in metadata:
        return False
    if port.vid is not None:
        return True
    # Some drivers omit USB IDs, particularly on Windows. Keep path/description
    # fallbacks for native Arduino USB and common USB-to-serial adapters.
    return any(token in metadata for token in (
        "arduino", "usb", "ttyacm", "ch340", "ch341", "ch910",
        "cp210", "ftdi", "uart bridge",
    ))


def select_serial_port(explicit_port=None):
    """Use an override, a unique USB candidate, or an interactive selection."""
    if explicit_port is not None:
        explicit_port = explicit_port.strip()
        if not explicit_port:
            raise PortSelectionError("The explicit serial port cannot be empty.")
        # Allow ports/aliases that an OS driver does not enumerate.
        return explicit_port

    ports = available_ports()
    if not ports:
        raise PortSelectionError(
            "No serial ports found. Connect the Arduino with a data-capable USB "
            "cable and check its USB serial driver. You can also specify --port."
        )

    candidates = [port for port in ports if is_controller_candidate(port)]
    if len(candidates) == 1:
        print(f"Auto-selected USB serial port: {describe_port(candidates[0])}")
        return candidates[0].device

    reason = (
        "Multiple possible controller ports found."
        if candidates else "No port could be identified automatically as a USB controller."
    )
    if not sys.stdin.isatty():
        devices = ", ".join(port.device for port in ports)
        raise PortSelectionError(
            f"{reason} Available ports: {devices}. "
            "Run interactively to choose, or use --port PORT."
        )

    print(reason)
    for index, port in enumerate(ports, start=1):
        print(f"  {index}: {describe_port(port)}")
    while True:
        try:
            answer = input("Select the shutter controller port number (q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise PortSelectionError("Port selection cancelled.") from exc
        if answer.lower() == "q":
            raise PortSelectionError("Port selection cancelled.")
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(ports):
            return ports[index - 1].device
        print(f"Enter a number from 1 to {len(ports)}, or q to cancel.")
