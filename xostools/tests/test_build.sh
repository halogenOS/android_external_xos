#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 The halogenOS Project
#
# Runs the build command against stubs of the build system and its output.
# Needs no source tree, no build and no network.

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp="$(mktemp -d)" || exit 1
trap 'rm -rf "$tmp"' EXIT

failures=0
function check() {
    local description="$1"
    shift
    if "$@"; then
        echo "ok: $description"
    else
        echo "FAIL: $description" >&2
        failures=$((failures + 1))
    fi
}

mkdir -p "$tmp/top/external"
ln -s "$repo" "$tmp/top/external/xos"
function gettop() {
    echo "$tmp/top"
}
THREAD_COUNT_BUILD=auto
source "$repo/xostools/xostools.sh" > /dev/null

version="Custom-17.0-20260926-dev"
unsigned_sum="04a83bcf7395630629d22bfeb8bac8f93f1ed56c1b993ec6591fda1c2ff259de"
signed_sum="c79c36b49c966d69ad0c58ced65cce1f0c99966fe010c96902d3a511efd58463"
calls="$tmp/calls"

# Stubs
function lunch() {
    echo "lunch $*" >> "$calls"
    TARGET_PRODUCT="$1"
    TARGET_BUILD_TYPE=release
    OUT="$tmp/out/$1"
}
function breakfast() {
    echo "breakfast $*" >> "$calls"
}
function m() {
    echo "m $*" >> "$calls"
    mkdir -p "$OUT/product/etc"
    if [ -n "$stub_no_build_prop" ]; then
        :
    elif [ -n "$stub_version" ]; then
        echo "ro.custom.version=$stub_version" > "$OUT/product/etc/build.prop"
    else
        echo "ro.custom.build_type=dev" > "$OUT/product/etc/build.prop"
    fi
    local goal
    for goal in "$@"; do
        case "$goal" in
            otapackage)
                [ -n "$stub_no_ota" ] || printf 'unsigned-ota' > "$OUT/$TARGET_PRODUCT-ota.zip"
            ;;
            target-files-dir)
                local target_files="$OUT/obj/PACKAGING/target_files_intermediates/$TARGET_PRODUCT-target_files"
                mkdir -p "$target_files/PRODUCT/etc" "$target_files/META"
                [ -n "$stub_no_build_prop" ] ||
                    cp "$OUT/product/etc/build.prop" "$target_files/PRODUCT/etc/build.prop"
            ;;
        esac
    done
}
function mmma() {
    echo "mmma $*" >> "$calls"
}
function python3() {
    echo "python3 $*" >> "$calls"
}
function sign_target_files_apks() {
    echo "sign_target_files_apks $*" >> "$calls"
}
function ota_from_target_files() {
    echo "ota_from_target_files $*" >> "$calls"
    printf 'signed-ota' > "${@: -1}"
}
function avbtool() {
    echo "avbtool $*" >> "$calls"
}

function reset_case() {
    rm -rf "$tmp/out" "$tmp/keys"
    : > "$calls"
    unset KEYS_DIR
    stub_version="$version"
    stub_no_ota=""
    stub_no_build_prop=""
}

function called() {
    grep -qxF "$1" "$calls"
}

function not_called() {
    ! grep -q "^$1\( \|$\)" "$calls"
}

function contains() {
    [[ "$1" == *"$2"* ]]
}

function not_contains() {
    [[ "$1" != *"$2"* ]]
}

# Succeeds when the first file is the unsigned OTA package as its only name
# and the second, the name the build gave it, is gone
function renamed() {
    [ ! -L "$1" ] && [ -f "$1" ] && [ "$(stat -c %h "$1")" -eq 1 ] &&
        [ "$(cat "$1")" = "unsigned-ota" ] && [ ! -e "$2" ] && [ ! -L "$2" ]
}

# Succeeds when the directory holds no package or checksum, hidden ones
# included, except the files named after it
function no_packages_in() {
    local dir="$1" file
    shift
    for file in "$dir"/* "$dir"/.*; do
        case "$file" in
            *.zip | *.sha256sum)
                contains " $* " " $file " || return 1
            ;;
        esac
    done
}

# Unsigned device build
reset_case
out="$tmp/out/aosp_dev"
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "unsigned build succeeds" [ $? -eq 0 ]
check "unsigned build asks for otapackage only" called "m --skip-soong-tests otapackage"
check "unsigned build does not sign" not_called sign_target_files_apks
check "unsigned package is renamed to the custom version" \
    renamed "$out/$version.zip" "$out/aosp_dev-ota.zip"
check "unsigned checksum names the bare file" \
    [ "$(cat "$out/$version.zip.sha256sum")" = "$unsigned_sum  $version.zip" ]
check "unsigned build prints the package path" \
    contains "$output" "$out/$version.zip"

# A KEYS_DIR that is not an absolute path makes an unsigned build
reset_case
KEYS_DIR=keys build full aosp_dev-cp2a-userdebug noclean > /dev/null 2>&1
check "relative KEYS_DIR build succeeds" [ $? -eq 0 ]
check "relative KEYS_DIR build asks for otapackage only" called "m --skip-soong-tests otapackage"
check "relative KEYS_DIR build does not sign" not_called sign_target_files_apks
check "relative KEYS_DIR package is renamed to the custom version" \
    renamed "$out/$version.zip" "$out/aosp_dev-ota.zip"

# Missing OTA package
reset_case
stub_no_ota=1
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "missing OTA package fails the build" [ $? -ne 0 ]
check "missing OTA package is reported" contains "$output" "No OTA package found"
check "missing OTA package leaves no named package" no_packages_in "$out"

# Missing custom version
reset_case
stub_version=""
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "missing custom version fails the build" [ $? -ne 0 ]
check "missing custom version is reported" \
    contains "$output" "No ro.custom.version found"
check "missing custom version leaves no named package" \
    no_packages_in "$out" "$out/aosp_dev-ota.zip"

# Missing product build.prop
reset_case
stub_no_build_prop=1
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "missing product build.prop fails the build" [ $? -ne 0 ]
check "missing product build.prop is reported as a missing custom version" \
    contains "$output" "No ro.custom.version found"
check "missing product build.prop prints no file error" \
    not_contains "$output" "No such file"

# Signed device build
reset_case
mkdir -p "$tmp/keys"
: > "$tmp/keys/releasekey.pk8"
target_files="$out/obj/PACKAGING/target_files_intermediates/aosp_dev-target_files"
KEYS_DIR="$tmp/keys" build full aosp_dev-cp2a-userdebug noclean > /dev/null 2>&1
check "signed build succeeds" [ $? -eq 0 ]
check "signed build asks for the target-files directory and the OTA tools" \
    called "m --skip-soong-tests target-files-dir otatools"
check "signed build does not ask for otapackage" [ ! -e "$out/aosp_dev-ota.zip" ]
check "signed build signs the target-files directory in place" \
    called "sign_target_files_apks -o -d $tmp/keys $target_files $target_files"
check "signed build names the signed package after the custom version" \
    called "ota_from_target_files -k $tmp/keys/releasekey $target_files $out/$version.zip"
check "signed checksum names the bare file" \
    [ "$(cat "$out/$version.zip.sha256sum")" = "$signed_sum  $version.zip" ]

# Signed device build with no custom version
reset_case
mkdir -p "$tmp/keys"
: > "$tmp/keys/releasekey.pk8"
stub_version=""
output=$(KEYS_DIR="$tmp/keys" build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "signed build with no custom version fails" [ $? -ne 0 ]
check "signed build with no custom version is reported" \
    contains "$output" "No ro.custom.version found"
check "signed build with no custom version makes no OTA package" \
    not_called ota_from_target_files
check "signed build with no custom version leaves no package" \
    no_packages_in "$out"

# Signed device build whose target-files directory holds no build.prop
reset_case
mkdir -p "$tmp/keys"
: > "$tmp/keys/releasekey.pk8"
stub_no_build_prop=1
output=$(KEYS_DIR="$tmp/keys" build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "signed build with no build.prop fails" [ $? -ne 0 ]
check "signed build with no build.prop is reported as a missing custom version" \
    contains "$output" "No ro.custom.version found"
check "signed build with no build.prop prints no file error" \
    not_contains "$output" "No such file"
check "signed build with no build.prop leaves no package" \
    no_packages_in "$out"

# A module build with no module and an absolute KEYS_DIR neither signs nor
# names a package
reset_case
mkdir -p "$tmp/keys"
: > "$tmp/keys/releasekey.pk8"
KEYS_DIR="$tmp/keys" build module aosp_dev-cp2a-userdebug noclean > /dev/null 2>&1
check "module build with absolute KEYS_DIR succeeds" [ $? -eq 0 ]
check "module build with absolute KEYS_DIR does not sign" \
    not_called sign_target_files_apks
check "module build with absolute KEYS_DIR names no package" \
    no_packages_in "$out" "$out/aosp_dev-ota.zip"

# SDK phone builds
for keys in "" "$tmp/keys"; do
    reset_case
    mkdir -p "$tmp/keys"
    : > "$tmp/keys/releasekey.pk8"
    out="$tmp/out/sdk_phone64_x86_64"
    KEYS_DIR="$keys" build full sdk_phone64_x86_64-cp2a-userdebug noclean > /dev/null 2>&1
    check "SDK phone build succeeds (KEYS_DIR '$keys')" [ $? -eq 0 ]
    check "SDK phone build asks for the default goal (KEYS_DIR '$keys')" \
        called "m --skip-soong-tests"
    check "SDK phone build does not sign (KEYS_DIR '$keys')" not_called sign_target_files_apks
    check "SDK phone build makes no package (KEYS_DIR '$keys')" no_packages_in "$out"
done

# Help text
help=$(xostools_help_build)
check "build help names the unsigned OTA package" contains "$help" "otapackage"
check "build help names the signed flow" contains "$help" "with the release keys"
check "build help says the signed flow signs the target-files directory" \
    contains "$help" "the target-files directory in place"
check "build help says SDK phone targets make no package" \
    contains "$help" "SDK phone (emulator) targets build the default goal"

if [ "$failures" -ne 0 ]; then
    echo "$failures check(s) failed" >&2
    exit 1
fi
echo "All checks passed"
