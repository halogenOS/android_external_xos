#!/usr/bin/env bash
set -e

# Reference: https://wiki.lineageos.org/signing_builds
#
# Generates the signing keys missing from KEYS_DIR. With --check it generates
# nothing, lists the key files missing from KEYS_DIR and fails when any is.

keys=(
    releasekey platform shared media networkstack testkey
    bluetooth sdk_sandbox verifiedboot nfc cts_uicc_2021
    cyngn-app verity gmscompat_lib
)

apexes=(
    com.android.adbd com.android.adservices
    com.android.adservices.api com.android.appsearch
    com.android.art com.android.bluetooth com.android.bt
    com.android.btservices com.android.cellbroadcast
    com.android.compos com.android.configinfrastructure
    com.android.connectivity.resources com.android.conscrypt com.android.crashrecovery
    com.android.devicelock com.android.extservices
    com.android.graphics.pdf com.android.hardware.authsecret
    com.android.hardware.biometrics.face.virtual
    com.android.hardware.biometrics.fingerprint.virtual
    com.android.hardware.boot com.android.hardware.cas
    com.android.hardware.contexthub com.android.hardware.dumpstate
    com.android.hardware.gatekeeper.nonsecure com.android.hardware.power
    com.android.hardware.thermal com.android.hardware.threadnetwork
    com.android.hardware.uwb com.android.hardware.vibrator com.android.telephonycore
    com.android.uprobestats
    com.android.hardware.neuralnetworks com.android.hardware.rebootescrow
    com.android.hardware.wifi com.android.healthfitness
    com.android.hotspot2.osulogin com.android.i18n com.android.ipsec
    com.android.media com.android.media.swcodec
    com.android.mediaprovider com.android.nearby.halfsheet
    com.android.networkstack.tethering com.android.neuralnetworks
    com.android.nfcservices com.android.npumanager
    com.android.ondevicepersonalization com.android.os.statsd
    com.android.permission com.android.profiling
    com.android.resolv com.android.rkpd
    com.android.runtime com.android.safetycenter.resources
    com.android.scheduling com.android.sdkext
    com.android.support.apexer com.android.telephony
    com.android.telephonymodules com.android.tethering
    com.android.tzdata com.android.uwb com.android.uwb.resources
    com.android.virt com.android.vndk.current
    com.android.vndk.current.on_vendor com.android.webapp com.android.wifi
    com.android.wifi.dialog com.android.wifi.resources
    com.google.pixel.camera.hal com.google.pixel.vibrator.hal
    com.qorvo.uwb
)

avb_key_sizes=(2048 4096)

# Prints the name of every key file that this script generates
key_files() {
    local key apex size
    for key in "${keys[@]}"; do
        echo "$key.pk8"
        echo "$key.x509.pem"
    done
    for apex in "${apexes[@]}"; do
        key="$apex.certificate.override"
        echo "$key.pk8"
        echo "$key.x509.pem"
        echo "$key.pem"
    done
    for size in "${avb_key_sizes[@]}"; do
        echo "avbkey_${size}.pem"
    done
}

if [ "$1" = "--check" ]; then
    missing=0
    while read -r file; do
        if [ ! -f "$KEYS_DIR/$file" ]; then
            echo "$KEYS_DIR does not contain the key file $file" >&2
            missing=1
        fi
    done < <(key_files)
    exit "$missing"
fi

echo "Keys dir: $KEYS_DIR"
mkdir -p "$KEYS_DIR"

checkSubject() {
    if [ -z "$KEYS_SUBJECT" ]; then
        echo "Please specify KEYS_SUBJECT"
        exit 1
    fi
}

rm -f "$KEYS_DIR/Android.bp"

cp ./development/tools/make_key "$KEYS_DIR"
sed -i 's|2048|4096|g' "$KEYS_DIR/make_key"
sed -i \
    -e 's/read -p "Enter password/#read -p "Enter password/' \
    -e 's/  password/#  password/' \
    "$KEYS_DIR/make_key"

echo "PRODUCT_CERTIFICATE_OVERRIDES := \\" > "$KEYS_DIR/keys.mk"

for key in "${keys[@]}"; do
    if [ ! -f "$KEYS_DIR/$key.pk8" ] || [ ! -f "$KEYS_DIR/$key.x509.pem" ]; then
        echo "Generating $key"
        checkSubject
        "$KEYS_DIR/make_key" "$KEYS_DIR/$key" "$KEYS_SUBJECT" || [ -f "$KEYS_DIR/$key.pk8" ]
    fi
done

for apex in "${apexes[@]}"; do
    key="$apex.certificate.override"
    if [ ! -f "$KEYS_DIR/$key.pk8" ] || [ ! -f "$KEYS_DIR/$key.x509.pem" ]; then
        echo "Generating $key"
        checkSubject
        "$KEYS_DIR/make_key" "$KEYS_DIR/$key" "$KEYS_SUBJECT" || [ -f "$KEYS_DIR/$key.pk8" ]
    fi
    # Key directories made before the PEM copy existed have only the pk8 file
    if [ ! -f "$KEYS_DIR/$key.pem" ]; then
        echo "Writing $key.pem"
        openssl pkcs8 -in "$KEYS_DIR/$key.pk8" -inform DER -nocrypt -out "$KEYS_DIR/$key.pem"
    fi
    echo "    $apex:$apex.certificate.override \\" >> "$KEYS_DIR/keys.mk"
    cat <<EOF >> "$KEYS_DIR/Android.bp"
android_app_certificate {
    name: "$apex.certificate.override",
    certificate: "$apex.certificate.override",
}

EOF
done

rm -f "$KEYS_DIR/make_key"

echo >> "$KEYS_DIR/keys.mk"

for size in "${avb_key_sizes[@]}"; do
    if [ ! -f "$KEYS_DIR/avbkey_${size}.pem" ]; then
        echo "Generating AVB key (RSA $size)"
        openssl genrsa -out "$KEYS_DIR/avbkey_${size}.pem" "$size"
    fi
done

cat <<'EOF' >> "$KEYS_DIR/keys.mk"
PRODUCT_DEFAULT_DEV_CERTIFICATE := $(KEYS_DIR)/releasekey
PRODUCT_MAINLINE_BLUETOOTH_SEPOLICY_DEV_CERTIFICATES := $(KEYS_DIR)/
PRODUCT_EXTRA_RECOVERY_KEYS := \
    build/make/target/product/security/testkey
PRODUCT_DEFAULT_AVB_KEY := $(KEYS_DIR)/avbkey_4096.pem
EOF

echo >> "$KEYS_DIR/keys.mk"


echo "Done."
