# swarmeval/display

The sandbox image for the `browser` and `computer` tools: a virtual screen, Chromium on it, and
`swarm-display`, which drives both. A case gets it through a profile's
[`display`](../../../docs/case-format.md#display); sandboxd starts it
([docs/services/sandboxd.md](../../../docs/services/sandboxd.md#display)); the tools are in
[docs/agent-runtime.md](../../../docs/agent-runtime.md#tools). Why it is shaped this way:
[spec/2026-10-07-browser-computer-use](../../../spec/2026-10-07-browser-computer-use/README.md).

```sh
mise run display:build      # docker build -t swarmeval/display:dev deploy/images/display
```

Building downloads Debian packages and Playwright's Chromium; running needs no network.

## What it holds

| Piece | Version | Role |
|---|---|---|
| `python:3.12-slim-bookworm` | | Base, and the Python `swarm-display` runs on |
| Xvfb, xauth | Debian | The screen, `:1`, with a cookie only `swarmdisplay` can read |
| matchbox-window-manager | Debian | Keeps every window maximized, without decorations |
| xdotool | Debian | Mouse and keyboard for `computer` |
| Playwright, its Chromium | 1.63.0 | The browser, driven through a pipe: no debugging port |
| Pillow | 12.3.0 | Screenshots of the whole screen |
| DejaVu, Noto CJK fonts | Debian | Text in pages, Chinese and Japanese included |

No terminal or file manager: the screen shows the browser and whatever a case adds.

## Contract

What sandboxd and the tools rely on. An image `FROM swarmeval/display` keeps it as long as it
does not remove these:

- The user `swarmdisplay` (uid 990) and `/run/swarm-display`, owned by it, mode `0700`.
- `/usr/local/bin/swarm-display`:
  - `start --width W --height H [--url U]`: starts the daemon (screen, window manager, start
    scripts, browser), waits until the browser is up, prints the daemon's pid, and exits 0.
    sandboxd runs it as `swarmdisplay` when it creates the sandbox.
  - `computer <json>` and `browser <json>`: one action, as the tools describe it. The text goes
    to stdout; an image, if the action took one, to `/run/swarm-display/out/screenshot.png`,
    which sandboxd collects and removes. Exit status 1 means the action failed; the reason is on
    stderr. They must run as `swarmdisplay`.
- `sh`, `sleep`, and `tr`, which every sandbox image needs.

## Adding a case's apps

Put executables in `/etc/swarm-display/start.d/`. The daemon runs them in name order, as
`swarmdisplay`, after the screen is up and before the browser opens, each with 30 seconds to
return; a non-zero exit fails the sandbox. Start servers in the background:

```dockerfile
FROM swarmeval/display:dev
COPY shop /opt/shop
COPY <<'EOF' /etc/swarm-display/start.d/10-shop
#!/bin/sh
cd /opt/shop && nohup python3 -m http.server 8080 --bind 127.0.0.1 >/dev/null 2>&1 &
EOF
RUN chmod 0755 /etc/swarm-display/start.d/10-shop
```

and point the profile's `display.url` at `http://127.0.0.1:8080/`. Their processes run as
`swarmdisplay`, so sandboxd counts them as the display's: they are not reported as `proc.*`
events.

## Limits

- The display's state, Chromium's profile included, lives in `/run/swarm-display`, outside every
  key path: nothing the browser writes is a file change, and downloads are not kept.
- Dialogs (`alert`, `confirm`) are dismissed.
- The browser locates elements by snapshot ref through the `aria-ref=` selector. Playwright 1.64
  makes `page.get_by_ref` public; move to it when upgrading.
