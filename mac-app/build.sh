#!/bin/zsh
set -euo pipefail
app_source_dir="${0:A:h}"
app_signing_identity="${CALL_DESK_SIGNING_IDENTITY:-}"
if [[ -z "$app_signing_identity" || "$app_signing_identity" == "-" ]]; then
  print -u2 -- "Set CALL_DESK_SIGNING_IDENTITY to an existing certificate identity; ad-hoc signing is not supported."
  exit 2
fi
app_staging_dir="$(mktemp -d /private/tmp/call-desk-build.XXXXXX)"
app_staged_bundle="$app_staging_dir/Call Desk.app"
app_keep_stage=0
trap 'if [[ "$app_keep_stage" != "1" ]]; then rm -rf -- "$app_staging_dir"; fi' EXIT
mkdir -p "$app_staged_bundle/Contents/MacOS" "$app_staged_bundle/Contents/Resources" "$app_source_dir/build"
xcrun swiftc -parse-as-library -swift-version 5 -O -target arm64-apple-macos14.0 \
  -framework AppKit -framework SwiftUI -framework Combine \
  "$app_source_dir/CallDesk.swift" "$app_source_dir/Messages.swift" -o "$app_staged_bundle/Contents/MacOS/CallDesk"
cp "$app_source_dir/Info.plist" "$app_staged_bundle/Contents/Info.plist"
plutil -lint "$app_staged_bundle/Contents/Info.plist"
# Sign outside Documents: its file provider recreates FinderInfo on .app roots.
# No quarantine, entitlements, or system security setting is changed.
codesign --force --sign "$app_signing_identity" "$app_staged_bundle"
codesign --verify --deep --strict "$app_staged_bundle"
"$app_staged_bundle/Contents/MacOS/CallDesk" --activation-self-test
"$app_staged_bundle/Contents/MacOS/CallDesk" --messaging-self-test
app_requirement="$(codesign -d -r- "$app_staged_bundle" 2>&1)"
if [[ "$app_requirement" == *"designated => cdhash "* ]]; then
  print -u2 -- "The app has a content-only signing identity; refusing to export it."
  exit 3
fi
print -r -- "$app_requirement"
if [[ "${CALL_DESK_STAGE_ONLY:-0}" != "1" ]]; then
  ditto -c -k --keepParent --norsrc --noextattr "$app_staged_bundle" "$app_source_dir/build/Call Desk.zip"
  unzip -tqq "$app_source_dir/build/Call Desk.zip"
  rm -rf -- "$app_staging_dir"
  print -r -- "Verified app ZIP: $app_source_dir/build/Call Desk.zip"
else
  app_keep_stage=1
  print -r -- "Verified staged bundle: $app_staged_bundle"
fi
