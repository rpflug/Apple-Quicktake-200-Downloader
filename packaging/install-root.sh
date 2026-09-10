#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Administrator authorization is required." >&2; exit 1; }
[ $# -eq 1 ] || { echo "Usage: install-root.sh PROJECT_DIRECTORY" >&2; exit 2; }

src=$1
version=0.1.0
pkgbase=$(cat "/usr/lib/modules/$(uname -r)/pkgbase" 2>/dev/null || true)
[ -n "$pkgbase" ] || { echo "Cannot identify the running kernel package." >&2; exit 1; }

missing=""
command -v dkms >/dev/null 2>&1 || missing="$missing dkms"
[ -e "/usr/lib/modules/$(uname -r)/build/Makefile" ] || missing="$missing ${pkgbase}-headers"
command -v python3 >/dev/null 2>&1 || missing="$missing python"
python3 -c 'import tkinter' >/dev/null 2>&1 || missing="$missing tk"
if [ -n "$missing" ]; then
    pacman -S --needed --noconfirm $missing
fi

install -d "/usr/src/quicktake-keyspan-$version"
install -m 0644 "$src/driver/"* "/usr/src/quicktake-keyspan-$version/"
install -m 0644 "$src/packaging/dkms.conf" "/usr/src/quicktake-keyspan-$version/dkms.conf"

dkms remove "quicktake-keyspan/$version" --all >/dev/null 2>&1 || true
dkms add "quicktake-keyspan/$version"
dkms build "quicktake-keyspan/$version" -k "$(uname -r)"
dkms install "quicktake-keyspan/$version" -k "$(uname -r)"

install -d /usr/lib/quicktake200 /usr/bin /usr/share/applications /usr/lib/udev/rules.d
install -m 0644 "$src/quicktake200/app.py" /usr/lib/quicktake200/quicktake200.py
install -m 0755 "$src/bin/quicktake200" /usr/bin/quicktake200
install -m 0644 "$src/quicktake200.desktop" /usr/share/applications/quicktake200.desktop
install -m 0644 "$src/packaging/99-quicktake-keyspan.rules" /usr/lib/udev/rules.d/99-quicktake-keyspan.rules

udevadm control --reload-rules
depmod -a
modprobe -r keyspan 2>/dev/null || true
modprobe ezusb
modprobe keyspan
udevadm trigger --subsystem-match=tty

echo "Apple QuickTake 200 Downloader installed successfully."
echo "Reconnect the Keyspan adapter, then launch it from the application menu."
