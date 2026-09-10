#!/usr/bin/env python3
"""Dependency-free Apple QuickTake 200 photo downloader for Linux."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import re
import select
import struct
import sys
import termios
import threading
import time

ENQ, ACK, NAK, DLE, STX, ETX, ETB, EOT = 0x05, 0x06, 0x15, 0x10, 0x02, 0x03, 0x17, 0x04

CMD_DOWNLOAD_IMAGE = bytes((0x00, 0x02))
CMD_VERSION = bytes((0x00, 0x09))
CMD_PICTURE_NAME = bytes((0x00, 0x0A))
CMD_PICTURE_COUNT = bytes((0x00, 0x0B))
CMD_PICTURE_SIZE = bytes((0x00, 0x17))
CMD_CHANGE_SPEED = bytes((0x01, 0x07))

SPEEDS = {
    9600: (termios.B9600, 0),
    19200: (termios.B19200, 5),
    38400: (termios.B38400, 6),
    57600: (termios.B57600, 7),
    115200: (termios.B115200, 8),
}


class QuickTakeError(RuntimeError):
    pass


def _bcc(data: bytes, control: int = ETX) -> int:
    result = control
    for byte in data:
        result ^= byte
    return result


def _packet(command: bytes, payload: bytes = b"") -> bytes:
    data = command + struct.pack("<H", len(payload)) + payload
    stuffed = data.replace(bytes((DLE,)), bytes((DLE, DLE)))
    return bytes((DLE, STX)) + stuffed + bytes((DLE, ETX, _bcc(data)))


class SerialPort:
    def __init__(self, path: str, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self.fd = -1
        self.speed = 9600

    def open(self) -> None:
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.configure(9600)
        # Assert DTR and RTS, as a normal serial application expects.
        bits = struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)
        fcntl.ioctl(self.fd, termios.TIOCMBIS, bits)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def configure(self, speed: int) -> None:
        if speed not in SPEEDS:
            raise QuickTakeError(f"Unsupported serial speed: {speed}")
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = termios.INPCK
        attrs[1] = 0
        attrs[2] &= ~(termios.CSIZE | termios.CSTOPB | termios.PARODD | termios.CRTSCTS)
        attrs[2] |= termios.CS8 | termios.PARENB | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = SPEEDS[speed][0]
        attrs[5] = SPEEDS[speed][0]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.speed = speed
        # The USA-28 applies serial settings through an asynchronous USB
        # control message. Do not queue data until the adapter has caught up.
        time.sleep(0.1)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _, writable, _ = select.select([], [self.fd], [], self.timeout)
            if not writable:
                raise QuickTakeError("Timed out writing to the camera")
            written = os.write(self.fd, view)
            view = view[written:]
        termios.tcdrain(self.fd)

    def read_exact(self, size: int) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + self.timeout
        while len(result) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QuickTakeError(f"Timed out waiting for {size - len(result)} camera byte(s)")
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if readable:
                chunk = os.read(self.fd, size - len(result))
                if chunk:
                    result.extend(chunk)
        return bytes(result)


class QuickTake200:
    def __init__(self, port: SerialPort):
        self.port = port

    def connect(self, speed: int = 57600) -> None:
        self.port.write(bytes((ENQ,)))
        reply = self.port.read_exact(1)[0]
        if reply != ACK:
            raise QuickTakeError(f"Camera handshake returned 0x{reply:02x}, expected ACK (0x06)")
        if speed != 9600:
            response = self.command(CMD_CHANGE_SPEED, bytes((SPEEDS[speed][1],)))
            if response != b"\x00":
                raise QuickTakeError(f"Camera rejected {speed}-baud mode: {response.hex()}")
            self.port.write(bytes((EOT,)))
            self.port.configure(speed)
            self.port.write(bytes((ENQ,)))
            if self.port.read_exact(1) != bytes((ACK,)):
                raise QuickTakeError("Camera did not answer after speed change")

    def disconnect(self) -> None:
        self.port.write(bytes((EOT,)))

    def command(self, command: bytes, payload: bytes = b"") -> bytes:
        self.port.write(_packet(command, payload))
        reply = self.port.read_exact(1)[0]
        if reply == NAK:
            raise QuickTakeError(f"Camera rejected command {command.hex(' ')}")
        if reply != ACK:
            raise QuickTakeError(f"Unexpected command reply: 0x{reply:02x}")
        return self._receive_response()

    def _receive_response(self) -> bytes:
        result = bytearray()
        last = False
        while not last:
            if self.port.read_exact(2) != bytes((DLE, STX)):
                raise QuickTakeError("Invalid response header")
            raw = bytearray()
            control = 0
            while True:
                byte = self.port.read_exact(1)[0]
                if byte != DLE:
                    raw.append(byte)
                    continue
                following = self.port.read_exact(1)[0]
                if following == DLE:
                    raw.append(DLE)
                elif following in (ETX, ETB):
                    control = following
                    break
                else:
                    raise QuickTakeError(f"Invalid escaped byte 0x{following:02x}")
            received_bcc = self.port.read_exact(1)[0]
            if received_bcc != _bcc(raw, control):
                self.port.write(bytes((NAK,)))
                raise QuickTakeError("Response checksum failed")
            if len(raw) < 4:
                raise QuickTakeError("Response packet is too short")
            declared = struct.unpack_from("<H", raw, 2)[0]
            payload = raw[4:]
            if declared != len(payload):
                raise QuickTakeError(
                    f"Response length mismatch: declared {declared}, received {len(payload)}"
                )
            result.extend(payload)
            self.port.write(bytes((ACK,)))
            last = control == ETX
        return bytes(result)

    def version(self) -> str:
        return self.command(CMD_VERSION).rstrip(b"\x00 \r\n").decode("ascii", "replace")

    def picture_count(self) -> int:
        data = self.command(CMD_PICTURE_COUNT)
        if len(data) != 2:
            raise QuickTakeError("Camera returned an invalid picture count")
        return struct.unpack("<H", data)[0]

    def picture_name(self, frame: int) -> str:
        data = self.command(CMD_PICTURE_NAME, struct.pack("<H", frame))
        name = data.rstrip(b"\x00 \r\n").decode("ascii", "replace")
        return re.sub(r"[^A-Za-z0-9._-]", "_", name) or f"IMAGE{frame:04d}.JPG"

    def picture_size(self, frame: int) -> int:
        data = self.command(CMD_PICTURE_SIZE, struct.pack("<H", frame))
        if len(data) != 4:
            raise QuickTakeError("Camera returned an invalid picture size")
        return struct.unpack("<I", data)[0]

    def download(self, frame: int) -> bytes:
        return self.command(CMD_DOWNLOAD_IMAGE, struct.pack("<H", frame))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download JPEG photos from an Apple QuickTake 200")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="serial port (default: /dev/ttyUSB0)")
    parser.add_argument("--speed", type=int, choices=sorted(SPEEDS), default=9600)
    parser.add_argument("--output", type=Path, default=Path("quicktake-photos"))
    parser.add_argument("--list", action="store_true", help="list photos without downloading")
    parser.add_argument("--frame", type=int, action="append", help="download only this frame; repeatable")
    parser.add_argument("--gui", action="store_true", help="open the graphical application")
    return parser.parse_args()


def _available_ports() -> list[str]:
    return [str(path) for path in sorted(Path("/dev").glob("ttyUSB*"))]


def gui_main() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print("The graphical interface needs the Omarchy 'tk' package. Run: sudo pacman -S tk", file=sys.stderr)
        return 1

    root = tk.Tk()
    root.title("Apple QuickTake 200 Downloader")
    root.geometry("620x430")
    root.minsize(520, 360)

    port_value = tk.StringVar(value=(_available_ports() or ["/dev/ttyUSB0"])[0])
    output_value = tk.StringVar(value=str(Path.home() / "Pictures" / "QuickTake 200"))
    status_value = tk.StringVar(value="Put the camera in PC Mode and connect it to Keyspan Port 1.")

    body = ttk.Frame(root, padding=18)
    body.pack(fill="both", expand=True)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(4, weight=1)

    ttk.Label(body, text="Serial port").grid(row=0, column=0, sticky="w", pady=5)
    port_box = ttk.Combobox(body, textvariable=port_value, values=_available_ports(), state="normal")
    port_box.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=5)

    ttk.Label(body, text="Save photos in").grid(row=1, column=0, sticky="w", pady=5)
    ttk.Entry(body, textvariable=output_value).grid(row=1, column=1, sticky="ew", padx=12, pady=5)
    ttk.Button(body, text="Choose…", command=lambda: output_value.set(
        filedialog.askdirectory(initialdir=output_value.get()) or output_value.get()
    )).grid(row=1, column=2, pady=5)

    progress = ttk.Progressbar(body, mode="determinate")
    progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(16, 7))
    ttk.Label(body, textvariable=status_value, wraplength=570).grid(
        row=3, column=0, columnspan=3, sticky="w", pady=(0, 10)
    )
    photos = tk.Listbox(body)
    photos.grid(row=4, column=0, columnspan=3, sticky="nsew")

    def set_busy(busy: bool) -> None:
        download_button.configure(state="disabled" if busy else "normal")
        refresh_button.configure(state="disabled" if busy else "normal")

    def finish(error: Exception | None = None) -> None:
        set_busy(False)
        if error:
            status_value.set(f"Could not communicate with the camera: {error}")
            messagebox.showerror("QuickTake 200", str(error))

    def camera_job(download: bool) -> None:
        serial = SerialPort(port_value.get())
        try:
            serial.open()
            camera = QuickTake200(serial)
            camera.connect(9600)
            count = camera.picture_count()
            root.after(0, lambda: (photos.delete(0, tk.END), progress.configure(maximum=max(count, 1), value=0)))
            destination = Path(output_value.get()).expanduser()
            if download:
                destination.mkdir(parents=True, exist_ok=True)
            for frame in range(1, count + 1):
                name = camera.picture_name(frame)
                size = camera.picture_size(frame)
                root.after(0, lambda f=frame, n=name, s=size: (
                    photos.insert(tk.END, f"{f:3}   {n}   ({s:,} bytes)"),
                    status_value.set(f"{'Downloading' if download else 'Reading'} photo {f} of {count}…"),
                ))
                if download:
                    image = camera.download(frame)
                    if len(image) != size or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
                        raise QuickTakeError(f"{name} was incomplete and was not saved")
                    temporary = (destination / name).with_suffix(Path(name).suffix + ".part")
                    temporary.write_bytes(image)
                    temporary.replace(destination / name)
                root.after(0, lambda f=frame: progress.configure(value=f))
            camera.disconnect()
            message = f"Downloaded {count} photos to {destination}" if download else f"Found {count} photos"
            root.after(0, lambda: status_value.set(message))
            if download:
                root.after(0, lambda: messagebox.showinfo("Download complete", message))
            root.after(0, finish)
        except (OSError, QuickTakeError) as error:
            root.after(0, lambda e=error: finish(e))
        finally:
            serial.close()

    def start(download: bool) -> None:
        set_busy(True)
        status_value.set("Connecting to the camera…")
        threading.Thread(target=camera_job, args=(download,), daemon=True).start()

    buttons = ttk.Frame(body)
    buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))
    refresh_button = ttk.Button(buttons, text="Show Photos", command=lambda: start(False))
    refresh_button.pack(side="left", padx=5)
    download_button = ttk.Button(buttons, text="Download All", command=lambda: start(True))
    download_button.pack(side="left")

    root.mainloop()
    return 0


def main() -> int:
    args = _arguments()
    if args.gui:
        return gui_main()
    port = SerialPort(args.port)
    try:
        port.open()
        camera = QuickTake200(port)
        camera.connect(args.speed)
        print(f"Connected: {camera.version()}")
        count = camera.picture_count()
        print(f"Photos in camera: {count}")
        frames = args.frame or list(range(1, count + 1))
        if args.list:
            for frame in frames:
                print(f"{frame:3}: {camera.picture_name(frame)} ({camera.picture_size(frame)} bytes)")
            camera.disconnect()
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        for frame in frames:
            name = camera.picture_name(frame)
            expected = camera.picture_size(frame)
            destination = args.output / name
            print(f"Downloading {frame}/{count}: {name} ({expected} bytes)")
            image = camera.download(frame)
            if len(image) != expected:
                raise QuickTakeError(
                    f"{name}: expected {expected} bytes, received {len(image)}; not saving partial file"
                )
            if not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
                raise QuickTakeError(f"{name}: camera response is not a complete JPEG")
            temporary = destination.with_suffix(destination.suffix + ".part")
            temporary.write_bytes(image)
            temporary.replace(destination)
            print(f"Saved {destination}")
        camera.disconnect()
        return 0
    except (OSError, QuickTakeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        port.close()


if __name__ == "__main__":
    raise SystemExit(main())
