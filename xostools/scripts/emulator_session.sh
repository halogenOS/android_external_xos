#!/usr/bin/env bash
# SPDX-FileCopyrightText: The halogenOS Project
# SPDX-License-Identifier: Apache-2.0
#
# Usage: emulator_session.sh <skin> <image directory>
#
# Starts the emulator on the product tree of the signing run over the regular
# build, with a writable system, syncs the output to it with adevice and waits
# until the emulator ends. The image directory is the product output directory
# of the signing run, which contains only what the signing run built again.

set -euo pipefail

skin="$1"
image_dir="$2"

# The session writes into a directory that is in neither build tree, so the
# emulator and adevice leave both builds as they are.
session_dir="${XDG_CACHE_HOME:-$HOME/.cache}/xostools/emulator/$TARGET_PRODUCT"
data_dir="$session_dir/data"
product_dir="$session_dir/product"
rm -rf "$session_dir"
mkdir -p "$data_dir" "$product_dir"

# The emulator, adevice and its fingerprint tool read one product tree: the
# files of the signing run over the files of the regular build, so that each
# image is the one of the signing run where it built the image again. The
# emulator opens and writes the files at the top of the tree, such as the
# images, the kernel, the .ini files and the .qcow2 overlays, so these are
# copies. adevice only reads the directories below, which are hard links where
# the build directory is on the file system of the session, and copies
# otherwise.
for dir in "$OUT" "$image_dir"; do
    link=()
    if [ "$(stat -c %d "$dir")" = "$(stat -c %d "$product_dir")" ]; then
        link=(-l)
    fi
    find "$dir" -mindepth 1 -maxdepth 1 -type d \
        -exec cp -a "${link[@]}" --remove-destination -t "$product_dir" {} +
    find "$dir" -mindepth 1 -maxdepth 1 ! -type d \
        -exec cp -a --remove-destination -t "$product_dir" {} +
done

echo "Launching emulator..."
ANDROID_PRODUCT_OUT="$product_dir" emulator -skin "$skin" -writable-system \
    -kernel "$product_dir/kernel-ranchu" -ramdisk "$product_dir/ramdisk-qemu.img" \
    -system "$product_dir/system-qemu.img" -vendor "$product_dir/vendor-qemu.img" \
    -initdata "$product_dir/userdata.img" -datadir "$data_dir" &
emu_pid=$!

echo "Waiting for emulator to appear..."
emu_serial=""
while [ -z "$emu_serial" ]; do
    emu_serial=$(adb devices | grep -o 'emulator-[0-9]*' | head -1) || true
    sleep 1
done
adb=(adb -s "$emu_serial")
echo "Found emulator: $emu_serial"

wait_for_boot() {
    "${adb[@]}" wait-for-device
    while [ "$("${adb[@]}" shell getprop sys.boot_completed 2> /dev/null)" != "1" ]; do
        sleep 2
    done
}

echo "Waiting for device to boot..."
wait_for_boot
echo "Device booted."

# Set up overlayfs for writable system
"${adb[@]}" root
"${adb[@]}" wait-for-device
echo "Setting up remount (this may reboot the device)..."
"${adb[@]}" remount -R
wait_for_boot
"${adb[@]}" root
"${adb[@]}" wait-for-device
"${adb[@]}" remount

echo "Pushing adevice_fingerprint to device..."
"${adb[@]}" push "$product_dir/system/bin/adevice_fingerprint" /system/bin/adevice_fingerprint

echo "Syncing build to device..."
ANDROID_PRODUCT_OUT="$product_dir" ANDROID_SERIAL=$emu_serial adevice update

echo "Emulator ready. The session ends when the emulator is closed; run emu again after a change."
wait "$emu_pid"
