# Shared Call Desk

A local MCP service and macOS menu-bar app for Google Voice calls and text messages. Service/plugin version **0.3.1**; native app **0.1.6**.

The service coordinates one shared browser across agents, keeps immutable message drafts, records one send intent, and preserves uncertain outcomes for inspection. It includes bounded incoming-call handling, an ElevenLabs audio worker, native Google Voice adapters, and an audit trail for independent send confirmation.

## Current capabilities and limits

- Outgoing calls through a local agent operating Google Voice and a routed audio bridge.
- Text inbox, conversation reads, exact drafts, one-shot sends, and independent confirmation with evidence.
- Structured errors identify the actual failure and the draft blocking another send to the same recipient.
- A native app displays connection, call, message and cleanup status.
- Automatic incoming answering is experimental; a successful complete incoming test has not been established.
- Native browser control still depends on an enrolled process/window and can require foreground access. Background browser control is proposed in [BACKGROUND-BROWSER-PLAN.md](BACKGROUND-BROWSER-PLAN.md).
- A confirmation or successful send does not release a browser reservation. Idle browser cleanup is a separate verified step.

This is a macOS local integration, not a hosted Google Voice API. It requires an authenticated browser, external native-control tools, and local audio configuration. Installing the plugin in a cloud-only client does not grant those capabilities.

## Setup

1. Create a Python environment and install the pinned dependencies:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.lock
   cp config.example.json config.json
   ```

2. Edit `config.json` locally. Set the absolute bridge path, ElevenLabs agent ID and 1Password secret reference. The bundled `voice_bridge.py` provides the routed audio transport used by the worker. Keep secrets in 1Password and local config out of Git.
3. Configure two virtual audio routes, using Loopback or an equivalent local setup. The example names are `Google Voice Return` and `Elle Call Mic`. Set Google Voice's speaker and microphone devices deliberately and verify both directions.
4. Install the external native tools required by this prototype: `agent-desktop`, the 1Password CLI, and Google Chrome. `agent-desktop` is a separately maintained local dependency and is not distributed here. Native enrollment must record the actual window, PID/process generation, Google account, Voice number and observed controls in private `state/google-voice-ui.json`. Do not invent enrollment or permission verification flags.
5. Build the exact-window helper:

   ```sh
   zsh build_native_focus.sh
   ```

6. Use the launch examples in [client-configs](client-configs). Replace `/absolute/path/to/shared-call-desk` with your checkout. The server command is `.venv/bin/python server.py`. Do not register duplicate server instances under different names to bypass shared ownership.

The initial audio policy in the example is `restore_original`. `keep_virtual` is a deliberate opt-in for persistent virtual defaults. Existing call records retain their saved policy.

## Agent skill and tools

The [shared-call-desk skill](plugin/skills/shared-call-desk/SKILL.md) documents the workflow. For Claude Code, link its directory into `~/.claude/skills/shared-call-desk`. Claude Desktop uses MCP server instructions; reconnect an existing client after tool changes.

Discover the active server schema. Version 0.3.1 exposes **22 tools**: call coordination, bounded incoming control, `text_inbox`, `text_read`, `text_draft`, `text_send`, `text_status`, `text_recover_browser`, and `text_confirm_sent`.

A send must match the user's authorized recipient and text. A timeout is not permission to send again. `text_confirm_sent` records independent operator evidence for an existing send intent and original owner/digest; it rejects drafts that were never attempted. `sent_observed` is automatic interface evidence, while `sent_confirmed` retains separately labeled operator evidence. Neither implies a read receipt.

## Native app

Build with your own existing code-signing identity:

```sh
CALL_DESK_SIGNING_IDENTITY="YOUR_SIGNING_IDENTITY" zsh mac-app/build.sh
.venv/bin/python install_mac_app.py --source "mac-app/build/Call Desk.zip"
```

The app locates the service at `~/CodingProjects/shared-call-desk` by default. Set `CALL_DESK_SERVICE_ROOT` in its launch environment to use another path. Installer registration is not a verification of future login recovery or current macOS permissions. Starting the app does not arm answering.

## Validation

```sh
.venv/bin/python -m pytest -q --disable-warnings
.venv/bin/python verify_mcp.py
```

The MCP smoke check uses isolated synthetic state, performs no real calls or texts, and checks structured errors and audited confirmation. Native helper and app builds run offline fixtures. Passing tests does not establish unattended reliability.

## Repository boundaries

This checkout contains source, tests, sanitized fixtures, example configuration and plugin definitions. Local configuration, credentials, browser profiles, message history, call recordings, native binaries, installation backups and account-specific verification records are excluded. The existing running installation is maintained separately until explicitly migrated to this checkout.
