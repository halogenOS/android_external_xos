#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 The halogenOS Project
#
# Runs the build command against stubs of the build system and its output.
# Needs no source tree, no build and no network.

# zsh stops at a pattern that matches nothing, which bash leaves as it is
[ -n "$ZSH_VERSION" ] && setopt null_glob

repo="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
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
test_signed_sum="6a550d1f72de6e17b482c984e8cbc53d7df66b84da685b516fdfc00356fc408f"
signed_sum="c79c36b49c966d69ad0c58ced65cce1f0c99966fe010c96902d3a511efd58463"
# The stubs of m and mmma run as programs, which read these from the environment
export tmp calls="$tmp/calls" stub_version stub_no_ota stub_no_build_prop stub_failing_signing

# Stubs of programs
mkdir -p "$tmp/bin"
export PATH="$tmp/bin:$PATH"
function stub() {
    printf '#!/usr/bin/env bash\n%s\n' "$2" > "$tmp/bin/$1"
    chmod +x "$tmp/bin/$1"
}
# The signing run writes the build.prop files and the OTA package, which need
# the build date or the keys, into a directory of its own. get_build_var
# returns that directory.
stub m '
echo "m $*" >> "$calls"
[[ " $* " == *" --signing-run "* ]] || exit 0
[ -n "$stub_failing_signing" ] && exit 1
product_out="$tmp/sealed/${OUT#"$tmp/out/"}"
mkdir -p "$product_out/product/etc"
if [ -n "$stub_no_build_prop" ]; then
    :
elif [ -n "$stub_version" ]; then
    echo "ro.custom.version=$stub_version" > "$product_out/product/etc/build.prop"
else
    echo "ro.custom.build_type=dev" > "$product_out/product/etc/build.prop"
fi
package=test-signed-ota
[[ " $* " == *" --signing-keys-dir="* ]] && package=signed-ota
if [[ " $* " == *" otapackage "* ]] && [ -z "$stub_no_ota" ]; then
    printf "%s" "$package" > "$product_out/$TARGET_PRODUCT-ota.zip"
fi
exit 0'
stub mmma 'echo "mmma $*" >> "$calls"'

# Stubs of shell functions
function lunch() {
    echo "lunch $*" >> "$calls"
    export TARGET_PRODUCT="$1"
    export TARGET_BUILD_TYPE=release
    export OUT="$tmp/out/$1"
}
function breakfast() {
    echo "breakfast $*" >> "$calls"
}
function get_build_var() {
    [ "$1" = SIGNED_PRODUCT_OUT ] && echo "$tmp/sealed/${OUT#"$tmp/out/"}"
}
function reset_case() {
    rm -rf "$tmp/out" "$tmp/sealed" "$tmp/keys"
    : > "$calls"
    unset KEYS_DIR
    stub_version="$version"
    stub_no_ota=""
    stub_no_build_prop=""
    stub_failing_signing=""
}

function called() {
    grep -qxF "$1" "$calls"
}

# Succeeds when the given lines are the only calls to m or mmma
function only_build_call() {
    [ "$(grep -E '^(m|mmma)( |$)' "$calls")" = "$1" ]
}

function contains() {
    [[ "$1" == *"$2"* ]]
}

# Succeeds when the text holds the third argument after the second
function contains_after() {
    local rest="${1#*"$2"}"
    [ "$rest" != "$1" ] && [[ "$rest" == *"$3"* ]]
}

function not_contains() {
    [[ "$1" != *"$2"* ]]
}

# Succeeds when the first file is a hard link to the second, the name the build
# gave the package, and holds the package named by the third argument
function named() {
    [ ! -L "$1" ] && [ "$1" -ef "$2" ] && [ "$(cat "$1")" = "$3" ]
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

# Device build with the test keys
reset_case
signed="$tmp/sealed/aosp_dev"
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "test key build succeeds" [ $? -eq 0 ]
check "test key build asks for files and the steps of otapackage, then for the signing run of otapackage" \
    only_build_call "m --skip-soong-tests --signing-goals=otapackage files
m --skip-soong-tests --signing-run otapackage"
check "test key package is named after the custom version" \
    named "$signed/$version.zip" "$signed/aosp_dev-ota.zip" test-signed-ota
check "test key checksum names the bare file" \
    [ "$(cat "$signed/$version.zip.sha256sum")" = "$test_signed_sum  $version.zip" ]
check "test key build prints the package path" \
    contains "$output" "OTA: $signed/$version.zip"

# Device build with the release keys
reset_case
mkdir -p "$tmp/keys"
: > "$tmp/keys/releasekey.pk8"
KEYS_DIR="$tmp/keys" build full aosp_dev-cp2a-userdebug noclean > /dev/null 2>&1
check "signed build succeeds" [ $? -eq 0 ]
check "signed build asks for files and the steps of otapackage, then for the signing run of otapackage with the keys" \
    only_build_call "m --skip-soong-tests --signing-goals=otapackage files
m --skip-soong-tests --signing-keys-dir=$tmp/keys --signing-run otapackage"
check "signed build names the signed package after the custom version" \
    named "$signed/$version.zip" "$signed/aosp_dev-ota.zip" signed-ota
check "signed checksum names the bare file" \
    [ "$(cat "$signed/$version.zip.sha256sum")" = "$signed_sum  $version.zip" ]

# KEYS_DIR that is not an absolute path, or holds no release keys
for keys in keys "$tmp/keys"; do
    reset_case
    mkdir -p "$tmp/keys"
    [ "$keys" = keys ] && error="KEYS_DIR needs an absolute path" || error="No release keys found"
    output=$(KEYS_DIR="$keys" build full aosp_dev-cp2a-userdebug noclean 2>&1)
    check "KEYS_DIR '$keys' fails the build" [ $? -ne 0 ]
    check "KEYS_DIR '$keys' is reported" contains "$output" "$error"
    check "KEYS_DIR '$keys' stops before the build" only_build_call ""
done

# Failing signing run
reset_case
stub_failing_signing=1
build full aosp_dev-cp2a-userdebug noclean > /dev/null 2>&1
check "failing signing run fails the build" [ $? -ne 0 ]
check "failing signing run leaves no named package" no_packages_in "$signed"

# Missing OTA package
reset_case
stub_no_ota=1
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "missing OTA package fails the build" [ $? -ne 0 ]
check "missing OTA package is reported" contains "$output" "No OTA package found"
check "missing OTA package leaves no named package" no_packages_in "$signed"

# Missing custom version
reset_case
stub_version=""
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "missing custom version fails the build" [ $? -ne 0 ]
check "missing custom version is reported" \
    contains "$output" "No ro.custom.version found"
check "missing custom version leaves no named package" \
    no_packages_in "$signed" "$signed/aosp_dev-ota.zip"

# Missing product build.prop
reset_case
stub_no_build_prop=1
output=$(build full aosp_dev-cp2a-userdebug noclean 2>&1)
check "missing product build.prop fails the build" [ $? -ne 0 ]
check "missing product build.prop is reported as a missing custom version" \
    contains "$output" "No ro.custom.version found"
check "missing product build.prop prints no file error" \
    not_contains "$output" "No such file"
check "missing product build.prop leaves no named package" \
    no_packages_in "$signed" "$signed/aosp_dev-ota.zip"

# module and mm with nothing named
for buildarg in module mm; do
    reset_case
    [ "$buildarg" = "mm" ] && missing="directory" || missing="module"
    output=$(build "$buildarg" aosp_dev-cp2a-userdebug noclean 2>&1)
    check "$buildarg build with nothing named fails" [ $? -ne 0 ]
    check "$buildarg build with nothing named names the missing $missing" \
        contains "$output" "No $missing named."
    check "$buildarg build with nothing named points at the full build" \
        contains "$output" '"build full" makes the package.'
    check "$buildarg build with nothing named stops before lunch" \
        [ ! -s "$calls" ]
done

# Named module or directory, with and without KEYS_DIR. The output
# directory of the signing run contains an OTA package from an earlier
# build, so naming would find something to work on.
out="$tmp/sealed/aosp_dev"
for keys in "" "$tmp/keys"; do
    for named in "module Settings" "mm packages/apps/Settings" "full Settings"; do
        buildarg="${named%% *}"
        name="${named#* }"
        [ "$buildarg" = "mm" ] && tool=mmma || tool=m
        label="$buildarg build of $name (KEYS_DIR '$keys')"
        reset_case
        mkdir -p "$tmp/keys"
        : > "$tmp/keys/releasekey.pk8"
        mkdir -p "$out/product/etc"
        echo "ro.custom.version=$version" > "$out/product/etc/build.prop"
        printf 'unsigned-ota' > "$out/aosp_dev-ota.zip"
        output=$(KEYS_DIR="$keys" build "$buildarg" aosp_dev-cp2a-userdebug "$name" noclean 2>&1)
        check "$label succeeds" [ $? -eq 0 ]
        check "$label builds what is named and skips the signing run" \
            only_build_call "$tool --skip-soong-tests $name"
        check "$label does not print a signed output" not_contains "$output" "Signed output"
        check "$label makes no package" \
            no_packages_in "$out" "$out/aosp_dev-ota.zip"
    done
done

# SDK phone builds
for keys in "" "$tmp/keys"; do
    reset_case
    mkdir -p "$tmp/keys"
    : > "$tmp/keys/releasekey.pk8"
    out="$tmp/sealed/sdk_phone64_x86_64"
    output=$(KEYS_DIR="$keys" build full sdk_phone64_x86_64-cp2a-userdebug noclean 2>&1)
    check "SDK phone build succeeds (KEYS_DIR '$keys')" [ $? -eq 0 ]
    check "SDK phone build asks for files and the steps of the default goal, then for the signing run of the default goal (KEYS_DIR '$keys')" \
        only_build_call "m --skip-soong-tests --signing-goals= files
m --skip-soong-tests ${keys:+--signing-keys-dir=$keys }--signing-run"
    check "SDK phone build prints the signed output (KEYS_DIR '$keys')" \
        contains "$output" "Signed output: $out"
    check "SDK phone build makes no package (KEYS_DIR '$keys')" no_packages_in "$out"
done

# The SDK phone message follows the thread count on every build but mm
for named in "full" "full Settings" "module Settings"; do
    buildarg="${named%% *}"
    name="${named#"$buildarg"}"
    reset_case
    output=$(build "$buildarg" sdk_phone64_x86_64-cp2a-userdebug $name noclean 2>&1)
    check "SDK phone $named build prints the SDK phone message after the thread count" \
        contains_after "$output" "Using adaptive job scaling." "Building for SDK phone"
done
reset_case
output=$(build mm sdk_phone64_x86_64-cp2a-userdebug packages/apps/Settings noclean 2>&1)
check "SDK phone mm build does not print the SDK phone message" \
    not_contains "$output" "Building for SDK phone"

# The devshell starts zsh, so the same checks run there. zsh rejects bash-only
# syntax such as local -n and the function carries on, here with an empty
# argument in place of --signing-run.
if [ -n "$BASH_VERSION" ]; then
    zsh -f "${BASH_SOURCE[0]}" || failures=$((failures + 1))
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures check(s) failed" >&2
    exit 1
fi
echo "All checks passed"
