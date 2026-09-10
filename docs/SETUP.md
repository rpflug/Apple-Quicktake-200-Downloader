# Setup, hardware notes, and troubleshooting

## Security and privileges

The downloader runs as the signed-in user. The one-time installer uses `pkexec` for a standard graphical administrator authorization. It does not store a password, create a setuid Python program, or grant broad serial-device access.

The udev rule applies only to serial ports belonging to USB vendor/product `06cd:010f` and explicitly applies systemd-logind's `uaccess` ACL to the active local session. Other USB and serial devices retain their normal permissions.

Installing the software does not permanently alter a USB port. A physical USB socket continues working normally with other devices. The persistent changes are files installed under `/usr`, a DKMS registration, and the narrow udev rule.

## Cable pinout

For the original QuickTake 200 cable:

| Mac mini-DIN-8 | Camera 2.5 mm TRS | Purpose |
|---|---|---|
| pin 3 | ring | computer transmit / camera receive |
| pin 5 | tip | computer receive / camera transmit |
| pins 4 and 8 | sleeve | signal ground |

Do not connect Keyspan Port 1 to Port 2 for camera use. The camera cable terminates at the camera's 2.5 mm socket.

## Confirm that Linux sees the adapter

```sh
lsusb | grep -i '06cd:'
ls -l /dev/ttyUSB*
```

The device can first appear as `06cd:0101`; the normal Linux firmware loader changes it to operational ID `06cd:010f`. There are no status lights on this model.

## Confirm the installed driver

```sh
modinfo keyspan | grep filename
dkms status
```

The module filename should resolve through the DKMS updates directory rather than the kernel's original copy.

## Common problems

### No camera response

- Confirm **PC Mode**, fresh batteries or external power, Port 1, and `/dev/ttyUSB0`.
- Reseat the camera's 2.5 mm plug; it can feel inserted before it is fully seated.
- Reconnect the Keyspan adapter after installation so the access rule is applied.
- Keep the speed at 9600. Higher-speed negotiation is not enabled by default because it was unreliable on the tested hardware.

### Permission denied

Log out and back in, then reconnect the adapter. Check that the current desktop session is active with `loginctl`. Do not run the downloader as root.

### Driver does not rebuild after an update

Ensure the matching headers are installed:

```sh
pkgbase=$(cat "/usr/lib/modules/$(uname -r)/pkgbase")
sudo pacman -S --needed dkms "${pkgbase}-headers"
sudo dkms autoinstall
```

### Secure Boot

Most Omarchy systems do not enable Secure Boot. If yours enforces signed third-party modules, DKMS module signing must be configured with a locally enrolled key before this driver can load.

## Remove the software

This is an explicit administrative operation:

```sh
sudo dkms remove quicktake-keyspan/0.1.0 --all
sudo rm -rf /usr/src/quicktake-keyspan-0.1.0
sudo rm -f /usr/bin/quicktake200 /usr/lib/quicktake200/quicktake200.py
sudo rm -f /usr/share/applications/quicktake200.desktop
sudo rm -f /usr/lib/udev/rules.d/99-quicktake-keyspan.rules
sudo udevadm control --reload-rules
sudo depmod -a
```

After rebooting, Linux returns to its original in-kernel Keyspan driver.

## Protocol and safety notes

The camera session uses ENQ/ACK framing and QuickTake's DLE-framed packets. Responses are checked using the protocol BCC. Downloads are checked against the size reported by the camera and must start and end with JPEG markers before being saved.

Only these read operations are implemented: version, picture count, picture name, picture size, and image download. Delete, erase-all, format, capture, and clock-setting commands are intentionally absent.
