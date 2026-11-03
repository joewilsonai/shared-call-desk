#!/bin/zsh
set -euo pipefail
focus_source_dir="${0:A:h}"
mkdir -p "$focus_source_dir/native-build"
focus_candidate="$(mktemp "$focus_source_dir/native-build/.call-desk-focus.XXXXXX")"
trap 'rm -f "$focus_candidate"' EXIT
xcrun clang -fobjc-arc -Wall -Wextra -O2 -mmacosx-version-min=14.0 \
  -framework AppKit -framework ApplicationServices -framework Foundation \
  "$focus_source_dir/native_focus.m" -o "$focus_candidate"
codesign --force --sign - "$focus_candidate"
codesign --verify --strict "$focus_candidate"
"$focus_candidate" --self-test
"$focus_candidate" --field-self-test
mv -f "$focus_candidate" "$focus_source_dir/native-build/call-desk-focus"
