# Exact native window recovery

`native-build/call-desk-focus` is the required local exact-window helper used
during explicit Show, incoming preparation, and guarded audio cleanup. Status polling and
ordinary reads do not focus the browser. Build with
`zsh build_native_focus.sh`; the build signs the binary and runs only its
offline identity/argument self-test. The installed general desktop toolkit is
not changed.

```
native-build/call-desk-focus --app 'Google Chrome' --window-id w-N \
  --pid PID --process-instance 'macos-proc-v1:SECONDS:MICROSECONDS' --budget-ms 6000
```

All identity arguments must come from the current enrolled window. The deadline
is 1–15000 ms. A caller must also enforce an outer subprocess timeout.

The helper holds the same per-user interaction lock as agent-desktop, verifies
the exact CoreGraphics window and process-start token, and activates that PID
before looking for its AX window. This avoids the existing focus implementation's
requirement that a window already be exposed through AX before its app is
activated. Only an AX window with the enrolled native window ID can be raised.
There is no title, sole-window, frontmost-window, or coordinate fallback. App
activation can temporarily surface that process's prior window; no input is sent
to it. A successful result requires the enrolled window to be on screen and
focused on two consecutive observations, with unchanged process identity.

`activation_dispatched` and `raise_dispatched` describe requests sent, not success.
On failure, do not infer focus or perform a subsequent click. On success, obtain
a fresh `pinned_reader.py` observation to verify the URL, account, and controls;
the helper itself has no browser-tab knowledge. Accessibility denial is returned
as `PERM_DENIED` without requesting or changing permissions. This helper does not
change audio routes or call state.

For read-only identity checks, prefix the same arguments with
`--inspect-identity`. This mode queries only the exact CoreGraphics window and
its libproc process-start token. It performs no AX calls, does not acquire an
interaction lock, and does not activate or raise a window. Success explicitly
reports `identity_verified: true` and both dispatch flags false.

`--inspect-page` adds a read-only native page URL observation. It requires
Accessibility permission, resolves only the enrolled AXWindow, and searches at
most 256 chrome nodes and 24 levels for exactly one outer AXWebArea. It stops
at each webarea, so nested iframe URLs cannot establish the page origin. A
successful receipt contains `page_complete: true`, `outer_webarea_count: 1`,
`page_role: AXWebArea`, and `page_url` obtained from the native AXURL attribute;
neither page titles nor text resembling URLs are accepted. Native identity is
checked before and after the page read. This mode does not focus, activate,
click, type, or change the browser. It is not yet used by the production reader;
the actual app-mode window must first supply a verified native URL receipt.

The subsequent `pinned_reader.py` observation has its own total deadline,
including these native identity checks before and after one existing
agent-desktop snapshot. The snapshot retains native window discovery and the accessibility
tree read. It defaults to 12000 ms. The enrollment file's optional
`native_read_budget_ms` configures this deadline; only integers from 1000 through
15000 are accepted. The reader subprocess timeout is that budget plus three
seconds. Identity and coverage requirements are unchanged. Existing bounded
audio-overlay read retries remain separate attempts; this setting adds no retries.

Release verification: compilation and the offline self-test passed. Effective
access and exact-window focus were observed from the installed app before the
0.1.4 retest. Its cooperative activation handoff then dispatched successfully,
but an unrelated Chrome Open dialog prevented exact-focus verification. A
separate browser profile has reached Voice; account, permissions, routes, and
enrollment remain under verification. A measured
native read of 7067 ms exceeded the former 6000 ms default, motivating the
12000 ms default. A complete 0.1.4 Show/Arm/Stop cycle and automatic incoming
answer remain unverified. The external helper is separate from the signed app.

## Message field identity handling

The private field mode must be the first argument: `--set-field-stdin`. It accepts the reviewed field value only through stdin. Chrome may expose the same accessibility element through multiple branches; the helper counts those references once by AX identity, rejects ancestor cycles, and preserves ambiguity checks for distinct controls. The 32 offline field fixtures include shared references, distinct identical-looking controls, and cycles.

Recipient chip cells can have no direct AX label. The helper requires one matching row and verifies the number in the cell's static-text descendants; a remove-button-only cell is not evidence of the number. Empty cells and conflicting numbers are rejected.
