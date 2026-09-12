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
    root.geometry("840x680")
    root.minsize(700, 560)

    colors = {
        "bg": "#0b0f0e",
        "panel": "#111816",
        "panel_dark": "#0e1513",
        "line": "#26342f",
        "ink": "#eef5f1",
        "muted": "#94a69e",
        "green": "#8ee6ad",
        "amber": "#f3c878",
        "red": "#ef897d",
        "button_ink": "#07100c",
    }
    root.configure(bg=colors["bg"])

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Tail.TFrame", background=colors["bg"])
    style.configure("Panel.TFrame", background=colors["panel_dark"])
    style.configure("Tail.TLabel", background=colors["bg"], foreground=colors["ink"], font=("Sans", 10))
    style.configure("Panel.TLabel", background=colors["panel_dark"], foreground=colors["ink"], font=("Sans", 10))
    style.configure("Muted.TLabel", background=colors["panel_dark"], foreground=colors["muted"], font=("Sans", 9))
    style.configure("Eyebrow.TLabel", background=colors["panel_dark"], foreground=colors["green"], font=("Sans", 8, "bold"))
    style.configure("Tail.TEntry", fieldbackground=colors["panel"], foreground=colors["ink"], bordercolor=colors["line"], lightcolor=colors["line"], darkcolor=colors["line"], padding=8)
    style.configure("Tail.TCombobox", fieldbackground=colors["panel"], background=colors["panel"], foreground=colors["ink"], arrowcolor=colors["green"], bordercolor=colors["line"], padding=7)
    style.map("Tail.TCombobox", fieldbackground=[("readonly", colors["panel"])], foreground=[("readonly", colors["ink"])])
    style.configure("Tail.TSpinbox", fieldbackground=colors["panel"], background=colors["panel"], foreground=colors["ink"], arrowcolor=colors["green"], bordercolor=colors["line"], padding=7)
    style.configure("Accent.TButton", background=colors["green"], foreground=colors["button_ink"], bordercolor=colors["green"], padding=(16, 10), font=("Sans", 10, "bold"))
    style.map("Accent.TButton", background=[("active", "#a8f0bf"), ("disabled", "#3e5948")], foreground=[("disabled", "#819088")])
    style.configure("Secondary.TButton", background=colors["panel"], foreground=colors["green"], bordercolor=colors["line"], padding=(14, 9), font=("Sans", 10, "bold"))
    style.map("Secondary.TButton", background=[("active", "#18231f"), ("disabled", "#101512")], foreground=[("disabled", "#53625b")])
    style.configure("Tail.Horizontal.TProgressbar", troughcolor=colors["panel"], background=colors["green"], bordercolor=colors["line"], lightcolor=colors["green"], darkcolor=colors["green"], thickness=8)
    style.configure("Tail.Vertical.TScrollbar", background=colors["panel"], troughcolor=colors["panel_dark"], bordercolor=colors["line"], arrowcolor=colors["muted"])

    port_value = tk.StringVar(value=(_available_ports() or ["/dev/ttyUSB0"])[0])
    output_value = tk.StringVar(value=str(Path.home() / "Pictures" / "QuickTake 200"))
    first_value = tk.IntVar(value=1)
    last_value = tk.IntVar(value=1)
    status_value = tk.StringVar(value="Put the camera in PC Mode and connect it to Keyspan Port 1.")
    camera_value = tk.StringVar(value="WAITING")
    count_value = tk.StringVar(value="—")

    body = ttk.Frame(root, padding=(28, 24, 28, 26), style="Tail.TFrame")
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(4, weight=1)

    masthead = ttk.Frame(body, style="Tail.TFrame")
    masthead.grid(row=0, column=0, sticky="ew", pady=(0, 20))
    masthead.columnconfigure(0, weight=1)
    tk.Label(masthead, text="LOCAL CAMERA TRANSFER", bg=colors["bg"], fg=colors["green"], font=("Sans", 9, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
    tk.Label(masthead, text="QuickTake", bg=colors["bg"], fg=colors["ink"], font=("Monospace", 34, "bold"), anchor="w").grid(row=1, column=0, sticky="w", pady=(0, 2))
    tk.Label(masthead, text="Move photographs from an Apple QuickTake 200—safely, locally, and without changing the camera.", bg=colors["bg"], fg=colors["muted"], font=("Sans", 11), anchor="w").grid(row=2, column=0, sticky="w")
    status_badge = tk.Label(masthead, textvariable=camera_value, bg=colors["panel_dark"], fg=colors["amber"], font=("Monospace", 9, "bold"), padx=14, pady=8, highlightthickness=1, highlightbackground=colors["line"])
    status_badge.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(20, 0))

    summary = tk.Frame(body, bg=colors["line"], highlightthickness=1, highlightbackground=colors["line"])
    summary.grid(row=1, column=0, sticky="ew", pady=(0, 18))
    summary.columnconfigure((0, 1, 2), weight=1, uniform="summary")
    for column, (heading, variable, note) in enumerate((
        ("CAMERA", camera_value, "Apple QuickTake 200"),
        ("PHOTOS", count_value, "Available to download"),
        ("SERIAL PORT", port_value, "Keyspan USA-28 · Port 1"),
    )):
        card = tk.Frame(summary, bg=colors["panel_dark"], padx=18, pady=14)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 1, 0))
        tk.Label(card, text=heading, bg=colors["panel_dark"], fg=colors["green"], font=("Sans", 8, "bold"), anchor="w").pack(fill="x")
        tk.Label(card, textvariable=variable, bg=colors["panel_dark"], fg=colors["ink"], font=("Monospace", 16, "bold"), anchor="w").pack(fill="x", pady=(5, 1))
        tk.Label(card, text=note, bg=colors["panel_dark"], fg=colors["muted"], font=("Sans", 8), anchor="w").pack(fill="x")

    controls = tk.Frame(body, bg=colors["panel_dark"], padx=18, pady=16, highlightthickness=1, highlightbackground=colors["line"])
    controls.grid(row=2, column=0, sticky="ew", pady=(0, 14))
    controls.columnconfigure(1, weight=1)
    tk.Label(controls, text="TRANSFER SETUP", bg=colors["panel_dark"], fg=colors["green"], font=("Sans", 8, "bold"), anchor="w").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 9))
    ttk.Label(controls, text="Serial port", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=5)
    port_box = ttk.Combobox(controls, textvariable=port_value, values=_available_ports(), state="normal", style="Tail.TCombobox")
    port_box.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(16, 0), pady=5)

    ttk.Label(controls, text="Save photos in", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=5)
    ttk.Entry(controls, textvariable=output_value, style="Tail.TEntry").grid(row=2, column=1, sticky="ew", padx=16, pady=5)
    ttk.Button(controls, text="Choose…", style="Secondary.TButton", command=lambda: output_value.set(
        filedialog.askdirectory(initialdir=output_value.get()) or output_value.get()
    )).grid(row=2, column=2, pady=5)

    range_row = ttk.Frame(controls, style="Panel.TFrame")
    range_row.grid(row=3, column=1, columnspan=2, sticky="w", padx=(16, 0), pady=(7, 0))
    ttk.Label(controls, text="Photo range", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(7, 0))
    ttk.Label(range_row, text="FROM", style="Eyebrow.TLabel").pack(side="left", padx=(0, 6))
    first_box = ttk.Spinbox(range_row, from_=1, to=9999, width=6, textvariable=first_value, style="Tail.TSpinbox")
    first_box.pack(side="left")
    ttk.Label(range_row, text="THROUGH", style="Eyebrow.TLabel").pack(side="left", padx=(16, 6))
    last_box = ttk.Spinbox(range_row, from_=1, to=9999, width=6, textvariable=last_value, style="Tail.TSpinbox")
    last_box.pack(side="left")

    progress = ttk.Progressbar(body, mode="determinate", style="Tail.Horizontal.TProgressbar")
    progress.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    status_label = tk.Label(body, textvariable=status_value, wraplength=760, bg=colors["bg"], fg=colors["muted"], font=("Sans", 9), anchor="w", justify="left")
    status_label.grid(row=5, column=0, sticky="ew", pady=(8, 10))

    inventory = tk.Frame(body, bg=colors["panel_dark"], highlightthickness=1, highlightbackground=colors["line"])
    inventory.grid(row=4, column=0, sticky="nsew")
    inventory.columnconfigure(0, weight=1)
    inventory.rowconfigure(1, weight=1)
    list_head = tk.Frame(inventory, bg=colors["panel_dark"], padx=15, pady=10)
    list_head.grid(row=0, column=0, columnspan=2, sticky="ew")
    tk.Label(list_head, text="CAMERA INVENTORY", bg=colors["panel_dark"], fg=colors["green"], font=("Sans", 8, "bold"), anchor="w").pack(side="left")
    tk.Label(list_head, text="Select consecutive rows to set a range", bg=colors["panel_dark"], fg=colors["muted"], font=("Sans", 8), anchor="e").pack(side="right")
    photos = tk.Listbox(inventory, selectmode=tk.EXTENDED, exportselection=False, bg=colors["panel_dark"], fg=colors["ink"], selectbackground="#294c3b", selectforeground=colors["green"], highlightthickness=0, borderwidth=0, activestyle="none", font=("Monospace", 10), relief="flat")
    photos.grid(row=1, column=0, sticky="nsew", padx=(15, 0), pady=(0, 12))
    scrollbar = ttk.Scrollbar(inventory, orient="vertical", command=photos.yview, style="Tail.Vertical.TScrollbar")
    scrollbar.grid(row=1, column=1, sticky="ns", padx=(6, 8), pady=(0, 12))
    photos.configure(yscrollcommand=scrollbar.set)

    def use_list_selection(_event=None) -> None:
        selected = photos.curselection()
        if not selected:
            return
        first_value.set(int(photos.get(selected[0]).split()[0]))
        last_value.set(int(photos.get(selected[-1]).split()[0]))

    photos.bind("<<ListboxSelect>>", use_list_selection)

    def set_camera_state(text: str, color: str) -> None:
        camera_value.set(text)
        status_badge.configure(fg=color)

    def set_busy(busy: bool) -> None:
        download_button.configure(state="disabled" if busy else "normal")
        range_button.configure(state="disabled" if busy else "normal")
        refresh_button.configure(state="disabled" if busy else "normal")

    def finish(error: Exception | None = None) -> None:
        set_busy(False)
        if error:
            status_value.set(f"Could not communicate with the camera: {error}")
            set_camera_state("CONNECTION ERROR", colors["red"])
            messagebox.showerror("QuickTake 200", str(error))

    def camera_job(download: bool, requested_range: tuple[int, int] | None = None) -> None:
        serial = SerialPort(port_value.get())
        try:
            serial.open()
            camera = QuickTake200(serial)
            camera.connect(9600)
            count = camera.picture_count()
            root.after(0, lambda: (
                set_camera_state("CONNECTED", colors["green"]),
                count_value.set(str(count)),
            ))
            reset_range = not download
            root.after(0, lambda: (
                photos.delete(0, tk.END),
                progress.configure(maximum=max(count, 1), value=0),
                first_box.configure(to=max(count, 1)),
                last_box.configure(to=max(count, 1)),
                last_value.set(count) if reset_range else None,
            ))
            destination = Path(output_value.get()).expanduser()
            if download:
                destination.mkdir(parents=True, exist_ok=True)
            if requested_range is None:
                frames = range(1, count + 1)
            else:
                first, last = requested_range
                if first < 1 or last < first or last > count:
                    raise QuickTakeError(f"Choose a range from 1 through {count}")
                frames = range(first, last + 1)
                root.after(0, lambda: progress.configure(maximum=last - first + 1, value=0))
            total = len(frames)
            for position, frame in enumerate(frames, 1):
                name = camera.picture_name(frame)
                size = camera.picture_size(frame)
                root.after(0, lambda f=frame, n=name, s=size, p=position, t=total: (
                    photos.insert(tk.END, f"{f:3}   {n}   ({s:,} bytes)"),
                    status_value.set(f"{'Downloading' if download else 'Reading'} photo {f} ({p} of {t})…"),
                ))
                if download:
                    image = camera.download(frame)
                    if len(image) != size or not image.startswith(b"\xff\xd8") or not image.endswith(b"\xff\xd9"):
                        raise QuickTakeError(f"{name} was incomplete and was not saved")
                    temporary = (destination / name).with_suffix(Path(name).suffix + ".part")
                    temporary.write_bytes(image)
                    temporary.replace(destination / name)
                root.after(0, lambda p=position: progress.configure(value=p))
            camera.disconnect()
            message = f"Downloaded {total} photos to {destination}" if download else f"Found {count} photos"
            root.after(0, lambda: status_value.set(message))
            if download:
                root.after(0, lambda: messagebox.showinfo("Download complete", message))
            root.after(0, finish)
        except (OSError, QuickTakeError) as error:
            root.after(0, lambda e=error: finish(e))
        finally:
            serial.close()

    def start(download: bool, use_range: bool = False) -> None:
        requested_range = None
        if use_range:
            try:
                selected = photos.curselection()
                if selected:
                    requested_range = (
                        int(photos.get(selected[0]).split()[0]),
                        int(photos.get(selected[-1]).split()[0]),
                    )
                else:
                    requested_range = (int(first_value.get()), int(last_value.get()))
            except (TypeError, ValueError, tk.TclError):
                messagebox.showerror("QuickTake 200", "Enter whole photo numbers for the range.")
                return
            if requested_range[0] < 1 or requested_range[1] < requested_range[0]:
                messagebox.showerror("QuickTake 200", "The Through number must be at least the From number.")
                return
        set_busy(True)
        status_value.set("Connecting to the camera…")
        set_camera_state("CONNECTING", colors["amber"])
        threading.Thread(target=camera_job, args=(download, requested_range), daemon=True).start()

    buttons = ttk.Frame(body, style="Tail.TFrame")
    buttons.grid(row=6, column=0, sticky="e", pady=(3, 0))
    refresh_button = ttk.Button(buttons, text="Show Photos", style="Secondary.TButton", command=lambda: start(False))
    refresh_button.pack(side="left", padx=5)
    range_button = ttk.Button(buttons, text="Download Range", style="Secondary.TButton", command=lambda: start(True, True))
    range_button.pack(side="left", padx=(0, 5))
    download_button = ttk.Button(buttons, text="Download All", style="Accent.TButton", command=lambda: start(True))
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
