# Apple QuickTake 200 Downloader

A small Linux application that downloads JPEG photographs from an Apple QuickTake 200 through an original Keyspan USA-28 USB-to-Mac-serial adapter.

The application has a simple graphical interface and a command-line interface. Its camera protocol code uses only the Python standard library. It never deletes or formats anything on the camera.

> **Hardware scope:** this release supports the original two-port Keyspan USA-28, USB IDs `06cd:0101` before firmware and `06cd:010f` after firmware. Other Keyspan models have not been tested.

## Why a custom driver is needed

The QuickTake 200 starts at 9600 baud with 8 data bits, even parity, and one stop bit. Linux's Keyspan USA-28 driver enables parity but historically leaves the adapter-specific parity-byte framing unimplemented. The included GPL-2.0 driver source supplies that missing transmit and receive handling.

## Install on Omarchy

Requirements are installed automatically from the normal Arch repositories. From this project folder, run:

```sh
./install.sh
```

A graphical administrator prompt appears once. It is unavoidable because Linux permits only an administrator to install a kernel module, a device-access rule, and a desktop launcher. The installer:

1. identifies the running Omarchy kernel;
2. installs DKMS, matching kernel headers, Python, and Tk when needed;
3. builds and installs the patched driver;
4. adds a `uaccess` rule limited to the Keyspan USA-28;
5. installs the application and desktop-menu entry; and
6. loads the driver.

Reconnect the adapter after installation. Ordinary use does not need `sudo` or another authorization prompt. DKMS attempts to rebuild the driver after kernel upgrades.

## Connect the camera

1. Connect the original QuickTake serial cable to **Port 1** of the Keyspan USA-28. Port 1 normally appears as `/dev/ttyUSB0`.
2. Insert the 2.5 mm plug fully into the camera.
3. Turn the camera on and select **PC Mode**.
4. Open **Apple QuickTake 200 Downloader** from the application menu.
5. Choose a destination and select **Download All**. To download a range, highlight consecutive photos in the list (or enter **From** and **Through** numbers), then select **Download Range**.

The default speed is 9600 baud because it was reliable in hardware testing. Downloads are written to a temporary `.part` file and renamed only after the complete JPEG passes size and boundary checks.

## Command line

```sh
quicktake200 --list
quicktake200 --output "$HOME/Pictures/QuickTake 200"
quicktake200 --port /dev/ttyUSB1 --list
```

Run `quicktake200 --help` for all options.

## Tested setup

- Apple QuickTake 200, camera firmware response `02.00,QT-200`
- original Apple mini-DIN-8 to 2.5 mm camera cable
- Keyspan USA-28, operational USB ID `06cd:010f`
- Port 1 mapped to `/dev/ttyUSB0`
- Omarchy with kernel `7.1.8-arch1-Watanare-T2-3-t2`
- 9600 baud, 8E1
- 21 photographs listed and the first 10 downloaded as valid 640×480 Apple QT-200 JPEGs before the camera battery ran low

See [docs/SETUP.md](docs/SETUP.md) for diagnostics, cable pinout, maintenance, and removal.

## Kernel upgrades

DKMS rebuilds the module for installed kernels when compatible headers are available. Kernel internal APIs can change; if a future build fails, keep the older working kernel available and open an issue with the kernel version and DKMS build log. A compiled `.ko` is deliberately not distributed because kernel modules must match the target kernel.

## Development

```sh
make test
python3 -m quicktake200 --help
```

The project is licensed under GPL-2.0-only because it incorporates and modifies the Linux Keyspan driver.
