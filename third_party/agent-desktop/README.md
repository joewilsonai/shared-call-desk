# Patched agent-desktop dependency

Call Desk's native adapter uses the separately published [Call Desk agent-desktop source fork](https://github.com/joewilsonai/call-desk-agent-desktop), based on [Vercel Labs' agent-desktop](https://github.com/vercel-labs/agent-desktop). A generic npm install is not a substitute for this fork build.

## Provenance and license

- Upstream revision: `5261b5287b5208e7c4bf3713df6f66faaaf2c4cc`.
- Local modifications: [`macos-window-identity.patch`](macos-window-identity.patch).
- Patch SHA-256: `00203b2c3647d58e7d2c15d2fcadb0143fac31bfcd72b860de0372c6c27b3100`.
- Upstream license: [Apache License 2.0](LICENSE), retained here for the modified source excerpts in the patch. Upstream attribution and copyright notices remain in the source.
- Modified files and behavior: macOS application-window discovery, AX window bridging, strict native-ID resolution and regression tests. Explicit native-window selection no longer falls back to a different window with the same title.

[provenance.json](provenance.json) records the base revision, patch hash and the reference binary hash. No binary is distributed here. Binary hashes can differ across build environments; the reference hash identifies the previously tested local build, not a cross-machine reproducibility guarantee.

The saved patch matches the modified source used for that local build. Its recorded local validation included formatting, Clippy, host library tests and a release build. A fresh application of the public patch is separately checked before publishing it. Installing on another Mac still needs local build and permission verification.

## Install the prebuilt macOS ARM64 release

Use this path on a matching Mac when the release binary can run under its normal OS permissions. This is a development build; no notarization is claimed. Source builds remain available below.

```sh
CALL_DESK_ROOT="$PWD"
CALL_DESK_DOWNLOAD="$(mktemp -d "${TMPDIR:-/tmp}/call-desk-desktop.XXXXXX")"
CALL_DESK_DESKTOP_ARCHIVE="$CALL_DESK_DOWNLOAD/agent-desktop-call-desk-macos-arm64.tar.gz"
curl --fail --location \
  'https://github.com/joewilsonai/call-desk-agent-desktop/releases/download/call-desk-macos-2026-11-02/agent-desktop-call-desk-macos-arm64.tar.gz' \
  --output "$CALL_DESK_DESKTOP_ARCHIVE"
printf '%s  %s\n' \
  '9cd075469d2c86c6eb0c35b3249483e9066ab50aefaf4922300df078c8a23dab' \
  "$CALL_DESK_DESKTOP_ARCHIVE" | shasum -a 256 -c -
mkdir "$CALL_DESK_DOWNLOAD/bin"
tar -xzf "$CALL_DESK_DESKTOP_ARCHIVE" -C "$CALL_DESK_DOWNLOAD/bin"
CALL_DESK_DESKTOP_BIN="$CALL_DESK_DOWNLOAD/bin"
```

The archive includes both executables, the upstream license, fork notes and a validation report naming the exact source commit. Continue with **Install without losing the existing tool** below. If this build is unsuitable for the target Mac, use the pinned source path instead; do not bypass OS security protections to force it to run.

## Build the published fork from source

These commands are for an installation agent acting on the user's request. Reuse an existing verified patched build when available. Otherwise build the pinned source rather than silently substituting a current upstream release.

Requirements: Git, Rust 1.89+ and Xcode command-line tools. Use existing tools. If Rust is missing and Homebrew is available, `brew install rust` supplies it; otherwise use the [official Rust installer](https://www.rust-lang.org/tools/install). Follow the operating tool's requirements for any protected OS installation or permission step.

Start from the Call Desk checkout. The example source directory must be absent; inspect an existing directory instead of overwriting it.

```sh
CALL_DESK_ROOT="$PWD"
CALL_DESK_DESKTOP_SRC="$HOME/.local/share/shared-call-desk/agent-desktop-src-b0da3bee"
mkdir -p "$HOME/.local/share/shared-call-desk"
git clone https://github.com/joewilsonai/call-desk-agent-desktop.git "$CALL_DESK_DESKTOP_SRC"
git -C "$CALL_DESK_DESKTOP_SRC" checkout --detach b0da3beed1cbb267dea81cd14491d4291a6f6dd3
```

The fork already contains the window/identity modifications. Do not apply the bundled patch again. The patch and upstream base revision remain here as provenance and for comparing the original changes with upstream.

Run the host-scoped checks and build. Unset `AGENT_DESKTOP_HOME` only for these test/build processes so each test retains its intended isolation:

```sh
cd "$CALL_DESK_DESKTOP_SRC"
env -u AGENT_DESKTOP_HOME cargo fmt --all -- --check
env -u AGENT_DESKTOP_HOME cargo test --locked \
  -p agent-desktop-core -p agent-desktop-macos -p agent-desktop -p agent-desktop-ffi --lib
env -u AGENT_DESKTOP_HOME cargo clippy --locked \
  -p agent-desktop-core -p agent-desktop-macos -p agent-desktop -p agent-desktop-ffi \
  --all-targets -- -D warnings
env -u AGENT_DESKTOP_HOME cargo build --release --locked -p agent-desktop
env -u AGENT_DESKTOP_HOME cargo build --release --locked -p agent-desktop-macos --bin agent-desktop-macos-helper
CALL_DESK_DESKTOP_BIN="$CALL_DESK_DESKTOP_SRC/target/release"
```

Stop if patch application or checks fail. Do not use a partially built or unpatched dependency while claiming the verified build is installed.

## Install without losing the existing tool

Use a versioned directory and preserve any existing command or symlink before changing command resolution. Do not stop other agents or native-control jobs without coordinating their active work.

```sh
CALL_DESK_DESKTOP_PREFIX="$HOME/.local/lib/shared-call-desk/agent-desktop/b0da3bee-call-desk"
mkdir -p "$CALL_DESK_DESKTOP_PREFIX" "$HOME/.local/bin"
install -m 755 "$CALL_DESK_DESKTOP_BIN/agent-desktop" \
  "$CALL_DESK_DESKTOP_PREFIX/agent-desktop"
install -m 755 "$CALL_DESK_DESKTOP_BIN/agent-desktop-macos-helper" \
  "$CALL_DESK_DESKTOP_PREFIX/agent-desktop-macos-helper"

if [ -e "$HOME/.local/bin/agent-desktop" ] || [ -L "$HOME/.local/bin/agent-desktop" ]; then
  CALL_DESK_DESKTOP_BACKUP="$HOME/.local/bin/agent-desktop.before-call-desk-$(date +%Y%m%dT%H%M%S)-$$"
  mv "$HOME/.local/bin/agent-desktop" "$CALL_DESK_DESKTOP_BACKUP"
fi
ln -s "$CALL_DESK_DESKTOP_PREFIX/agent-desktop" "$HOME/.local/bin/agent-desktop"
"$HOME/.local/bin/agent-desktop" --version
"$HOME/.local/bin/agent-desktop" skills get desktop
"$HOME/.local/bin/agent-desktop" skills get platform
```

Make sure `~/.local/bin` is on the intended agent's PATH. Verify command resolution, record the new binary hash and retain the backup path. If linking fails after the backup move, restore that backup before continuing.

Inspect permissions with `agent-desktop permissions`. Use `agent-desktop permissions --guide` for the normal macOS permission workflow when required. A successful build, version string or settings entry does not prove effective access. Verify a harmless exact-window read before calling the browser adapter ready; no text or call is authorized by this dependency installation.
