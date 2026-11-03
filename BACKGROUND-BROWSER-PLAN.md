# Background Google Voice control

Proposed next adapter; not implemented or verified.

Call Desk should own a dedicated persistent browser profile and control the Google Voice page through Playwright rather than macOS focus, AX references, and keyboard events. The shared service remains the only owner of the profile and browser reservation. Agents keep using the existing MCP tools and durable draft/send records.

## Delivery sequence

1. Prove inbox reads in a dedicated background browser while Joe works in another app. Preserve exact source account and Voice number checks. Do not copy credentials or cookies into logs.
2. Prove one explicitly authorized test send and an incoming reply while another app stays foreground. Preserve draft digest, one send intent, and no retries after uncertain dispatch.
3. Prove calls and both virtual audio directions in a background headed browser before considering headless mode. Headless browser support alone does not establish Google Voice login, microphone, playback, or call reliability.
4. Add browser ownership, local health monitoring, bounded restart recovery, and an Open sign-in window action. Sign-in challenges require user attention; do not bypass them.
5. Retire the native adapter only after the background version passes account mismatch, stale page, crash-after-send, duplicate request, and concurrent call/message tests. Keep rollback available.

Prefer a local pipe when the browser is service-launched. If CDP attachment is needed, keep its endpoint local and private and use only the dedicated non-default profile. Never expose an authenticated browser debug endpoint to a network. Chrome requires a non-default data directory for remote debugging in modern versions.

The Call Desk window should show browser, sign-in, messaging and audio health separately. It should stay out of the foreground unless Joe opens it or authentication needs attention. Running on this Mac still depends on the Mac being awake; independent availability would need a dedicated always-on host.

Sources checked for this design:
- https://playwright.dev/python/docs/api/class-browsertype
- https://developer.chrome.com/blog/remote-debugging-port
