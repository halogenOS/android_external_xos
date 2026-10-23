#!/usr/bin/env bash
# SPDX-FileCopyrightText: The halogenOS Project
# SPDX-License-Identifier: Apache-2.0
#
# Usage: emulator_session.sh <skin> <image directory>
#
# Starts the emulator on the images of the signing run in the image directory,
# the product output directory of the signing run, with a writable system,
# syncs the output to it with adevice and waits until the emulator ends.

set -euo pipefail

skin="$1"
image_dir="$2"

# The emulator reads the images of the signing run and the other files of the
# build, such as the kernel, from one product directory. The files of the
# signing run replace those of the regular build.
emulator_dir="$image_dir/emulator"
rm -rf "$emulator_dir"
mkdir -p "$emulator_dir/system"
for dir in "$OUT" "$image_dir"; do
    find "$dir" -maxdepth 1 -type f -exec ln -f -t "$emulator_dir" {} +
done
ln -f "$image_dir/system/build.prop" "$emulator_dir/system/build.prop"

echo "Launching emulator..."
ANDROID_PRODUCT_OUT="$emulator_dir" emulator -skin "$skin" -writable-system &
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
"${adb[@]}" push "$OUT/system/bin/adevice_fingerprint" /system/bin/adevice_fingerprint

echo "Syncing build to device..."
ANDROID_SERIAL=$emu_serial adevice update

echo "Emulator ready. The session ends when the emulator is closed; run emu again after a change."
wait "$emu_pid"
