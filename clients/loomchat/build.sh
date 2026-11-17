#!/usr/bin/env bash
# LoomChat APK, no Gradle. Needs an Android SDK (platform 34, build-tools 35.0.0) at $ANDROID_SDK.
# A signing key is generated in keys/ on first run (git-ignored).
#: aapt2 -> javac -> d8 -> zipalign -> apksigner
set -euo pipefail
ICI=$(cd "$(dirname "$0")" && pwd)
VERSION_CODE=1
VERSION_NAME=0.1.0
SDK="${ANDROID_SDK:-$HOME/android-sdk}"
export PATH="$SDK/build-tools/35.0.0:$PATH"
JAR="$SDK/platforms/android-34/android.jar"
BT="$SDK/build-tools/35.0.0/lib"
OUT="$ICI/build"; T="$OUT/tmp"; CLE="$ICI/keys/loomchat.p12"
rm -rf -- "${T:?}"; mkdir -p "$T/classes" "$T/gen" "$T/dex"
if [ ! -f "$CLE" ]; then mkdir -p "$ICI/keys"; head -c 24 /dev/urandom | base64 > "$ICI/keys/password"
  keytool -genkeypair -keystore "$CLE" -storetype PKCS12 -storepass "$(cat "$ICI/keys/password")" -alias loomchat \
    -keyalg RSA -keysize 3072 -validity 10000 -dname "CN=LoomChat"; fi
aapt2 compile --dir "$ICI/src/res" -o "$T/res.zip"
aapt2 link -I "$JAR" --manifest "$ICI/src/AndroidManifest.xml" --min-sdk-version 26 --target-sdk-version 34 \
  --version-code "$VERSION_CODE" --version-name "$VERSION_NAME" -A "$ICI/src/assets" --java "$T/gen" -o "$T/base.apk" "$T/res.zip"
find "$ICI/src/java" "$T/gen" -name '*.java' > "$T/sources.txt"
javac --release 17 -encoding UTF-8 -Xlint:all,-options,-serial -Werror -cp "$JAR" -d "$T/classes" @"$T/sources.txt"
find "$T/classes" -name '*.class' > "$T/classes.txt"
java -cp "$BT/d8.jar" com.android.tools.r8.D8 --release --min-api 26 --lib "$JAR" --output "$T/dex" @"$T/classes.txt"
cp "$T/base.apk" "$T/ns.apk"; (cd "$T/dex" && zip -q "$T/ns.apk" classes.dex)
zipalign -p -f 4 "$T/ns.apk" "$T/al.apk"
APK="$OUT/loomchat-$VERSION_NAME.apk"
java -jar "$BT/apksigner.jar" sign --ks "$CLE" --ks-pass "file:$ICI/keys/password" --out "$APK" "$T/al.apk"
java -jar "$BT/apksigner.jar" verify "$APK" && aapt2 dump badging "$APK" | grep -E "^package|application-label:"
sha256sum "$APK"
