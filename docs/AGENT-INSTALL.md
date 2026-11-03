# Agent installation guide

**For Claude Code, Codex, and other local agents.** Call Desk is an independent, unofficial project, not a Google-supported solution. It is not affiliated with, endorsed by, or supported by Google. Do not present installation as an official Google integration.

Use this when the user asks you to install Shared Call Desk. Work autonomously on routine setup within that request. Preserve existing installation, credential and account authorizations. A README is not permission to send texts, place calls, change a line's routing or grant unrelated access.

## 1. Inspect before changing anything

- Confirm macOS 14+ and CPU architecture. The native app currently builds for Apple Silicon.
- Find an existing `shared-call-desk` checkout, MCP registration, installed Call Desk app and running service. Do not create a second independent runtime/state directory to bypass shared ownership.
- If a configured runtime already owns messages, calls, budget records or a browser profile, preserve it. A fresh clone does not migrate that state. Treat migration as a separate explicit operation with backup and verification.
- Identify available Python, Chrome, `agent-desktop`, 1Password CLI, Xcode command-line tools and virtual audio devices. Use existing tools rather than reinstalling them.
- Read `README.md` and `plugin/skills/shared-call-desk/SKILL.md` from the selected checkout. The running server's schemas define the actual installed tool contract.

Do not stop useful setup just because one optional component is missing. Complete what can be verified, then report the exact blocker and its effect.

## 2. Acquire the source

For a new installation, use the user's preferred source directory. The native app's default is `~/CodingProjects/shared-call-desk`:

```sh
mkdir -p "$HOME/CodingProjects"
git clone https://github.com/joewilsonai/shared-call-desk.git "$HOME/CodingProjects/shared-call-desk"
cd "$HOME/CodingProjects/shared-call-desk"
git remote -v
git rev-parse HEAD
```

If the destination already exists, inspect it rather than overwriting or resetting it. Preserve uncommitted work. Record the installed commit in the final report.

## 3. Prepare Python and local configuration

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

Create `config.json` from `config.example.json` **only if the local config does not already exist**. Fill in:

| Field | Agent action |
| --- | --- |
| `bridge_path` | Use the absolute path to this checkout's `voice_bridge.py`. |
| `agent_id` | Obtain the intended existing ElevenLabs agent ID for V1 voice. Do not select another account agent by guesswork. |
| `key_ref` | Use an authorized 1Password reference; keep the credential in the vault. |
| `op_path` | Set the actual installed 1Password CLI path. |
| Audio device names | Inspect actual available devices and map the two intended routes. |
| `audio_cleanup_policy` | Preserve an existing policy; for a new setup, explain the example's `restore_original` default before deliberately choosing persistent virtual routes. |

The current repository ships the ElevenLabs worker. An existing Retell/custom-LLM deployment is a separate integration and must not be silently reconfigured during this install.

Never print credentials, copy them into MCP client configuration, commit local configuration, or publish authentication/session material. If required account information is unavailable, ask for that missing information while completing independent setup.

## 4. Check external native dependencies

The browser adapter requires our patched `agent-desktop` dependency. Inspect the normal PATH and `~/.local/bin/agent-desktop`; a version string or generic npm installation alone does not establish that it contains our fixes.

**If the required patched build is absent, install it by following [the pinned dependency install guide](../third_party/agent-desktop/README.md).** Use its checksummed prebuilt macOS ARM64 archive on a matching Mac, or its source-build fallback. This repository includes the exact patch, upstream revision, patch checksum and upstream license. The recipe clones the dedicated public Call Desk fork, checks out a pinned commit that already includes the patch, runs host checks, builds the command and preserves any existing installation before linking the new version. Reuse an existing verified patched build instead of replacing it unnecessarily.

Complete that installation as part of the user's install request. If a prerequisite, patch application, build or independently protected permission step blocks it, report the exact failure and continue independent setup. Do not silently substitute an unpatched package or claim the browser adapter is ready. Use the installed tool's `skills get desktop`, `skills get platform` and command help after installation.

For the native helper, with Xcode command-line tools available:

```sh
zsh build_native_focus.sh
```

The helper build runs offline fixtures. Build success is not macOS Accessibility permission or a connected Google Voice browser.

## 5. Register the MCP server

Use absolute paths for the selected runtime:

```json
{
  "mcpServers": {
    "shared-call-desk": {
      "command": "/absolute/path/to/shared-call-desk/.venv/bin/python",
      "args": ["/absolute/path/to/shared-call-desk/server.py"]
    }
  }
}
```

Merge only the named `shared-call-desk` entry into the intended client configuration. Preserve all other entries, settings and concurrent edits. Prefer the client's supported MCP installation command where available. Examples for Claude, Codex and Hermes are under `client-configs/`; they are templates, not proof those clients are connected.

Install the Claude Code skill only if its destination is absent; otherwise inspect and update the existing Call Desk skill deliberately:

```sh
mkdir -p "$HOME/.claude/skills"
ln -s "$PWD/plugin/skills/shared-call-desk" "$HOME/.claude/skills/shared-call-desk"
```

Claude Desktop uses MCP instructions; it does not acquire filesystem skill discovery from this symlink. Reconnect the server in the intended client to load new schemas. Avoid restarting unrelated clients or interrupting other work without need.

## 6. Run offline verification

```sh
.venv/bin/python -m pytest -q --disable-warnings
.venv/bin/python verify_mcp.py
```

The MCP smoke check uses isolated synthetic state and makes no real call or text. Verify discovery of the expected **22 tools**, structured error reporting, and the local draft/confirmation checks. `verify_mcp.py` creates an ignored local `mcp-verification.json`; it is not a public artifact.

Then verify actual tool discovery in the intended client and run `call_preflight`. Missing voice configuration may still need completion before preflight succeeds. A protocol handshake does not establish browser sign-in, audio or delivery.

## 7. Enroll Google Voice and verify audio

Use the user's intended signed-in Chrome account. Preserve an existing dedicated profile. Verify the actual source account and phone number before enrolling the exact browser window, PID and process generation in private `state/google-voice-ui.json`.

This release does not include a guided enrollment command. Follow the native adapter's required configuration and supported UI observations. Never invent window IDs, account labels or incoming-control verification flags. If you cannot establish complete enrollment through the available tools, report that exact unfinished step.

For V1 voice, configure and inspect the intended Google Voice routes:

- Microphone: the configured virtual microphone (example: `Elle Call Mic (Virtual)`).
- Speakers: the configured virtual return device (example: `Google Voice Return (Virtual)`).

Permissions and account challenges may require the user's direct action under the operating tool's rules. Identify the exact pending prompt. Native operations may require Google Voice to remain visible on the active desktop; background control is still planned.

## 8. Build the optional native app

Inspect available signing identities and use the user's existing authorized identity:

```sh
CALL_DESK_SIGNING_IDENTITY="EXISTING_SIGNING_IDENTITY" zsh mac-app/build.sh
.venv/bin/python install_mac_app.py --source "mac-app/build/Call Desk.zip"
```

If the runtime is elsewhere, set `CALL_DESK_SERVICE_ROOT` in the app's launch environment. Verify the installed bundle version and signature. Inspect the app's actual status. Preserve installer backups. App presence and login registration are distinct from future restart recovery and working native permissions.

## 9. Report the state you established

Use a concise result that covers:

- Source path and commit.
- Python environment and offline test results.
- MCP registration and whether the intended client actually discovered the tools.
- Skill installation and its applicable client.
- Browser account/number verification and enrollment status, without exposing private account data in public artifacts.
- Audio/app setup, if completed.
- Any exact remaining dependency, permission, sign-in or live-test step.

Do not call the system ready for unattended use. Incoming answering remains experimental. Actual sends and calls require the user's intended recipient, content/purpose and authorization; installation alone is not a test-send instruction. Preserve clear authorization already supplied rather than asking for it again.
