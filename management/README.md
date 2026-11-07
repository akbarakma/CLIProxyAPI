# Management Center customization

This folder adds a Keepalive section between the Dashboard metrics and traffic chart.
It stays in the CLIProxyAPI fork, without a separate frontend fork.

The Routing page adds provider filters above the existing board. API keys and
OAuth credentials for the same provider share a filter. Each OpenAI-compatible
gateway has its own filter, and All providers retains the combined board.
Switching filters keeps the full unsaved draft. Dragging, keyboard moves,
priority calculation, shared policy, and the Save review still use that draft.

The section reads the five systemd timers and their result files every 15 seconds.
Its switches persist through systemd `enable --now` or `disable --now` on a named timer.
A disabled timer leaves an existing ping running.

`server.py` binds localhost on 8319. The API uses the current bcrypt hash from
`config.yaml`, so the existing management key works and rotations take effect immediately.
The account list is fixed in code. Requests cannot select an arbitrary systemd unit.

## Build

Run from the checkout root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 management/build.py
```

The builder downloads the exact source commit and verifies the checksum in
`upstream.json`. It applies `ui/` and `strings.json`, runs the full upstream test,
lint and build commands, then atomically replaces the single HTML artifact in
`.local/management-panel/`. Bun is pinned and runs through npm without a global install.
The host service uses the installed `python3-bcrypt` and `python3-yaml` packages.

The pinned upstream cooldown test compares an unescaped apostrophe against an
HTML attribute. The builder corrects that assertion to use React's escaped value.
No upstream tests are skipped.

English keepalive labels are registered in each existing locale. Provider filter
labels are translated in `routing-strings.json`. The rest of the panel keeps its
upstream translations.

## Service installation

Check port 8319 and every container's saved port bindings before the first start.
Link the service into the user manager:

```bash
ln -s "$PWD/management/cliproxyapi-management.service" "$HOME/.config/systemd/user/cliproxyapi-management.service"
systemctl --user daemon-reload
systemctl --user enable --now cliproxyapi-management.service
sudo tailscale serve --bg --yes --https=8444 --set-path=/v8/management/keepalive http://127.0.0.1:8319/v8/management/keepalive
sudo tailscale serve --bg --yes --https=8444 --set-path=/management.html http://127.0.0.1:8319/management.html
```

A panel rebuild takes effect on reload. A Python service change needs a restart
of the named management service. The proxy container continues running.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest management.test_server management.test_systemd -v
PYTHONDONTWRITEBYTECODE=1 python3 deploy/verify.py --external --live
```

The systemd test creates its own unique timer and ping service. It checks repeated
on/off requests, persistent settings, and a running ping that survives pausing.
It cleans only those two test units. It never toggles a production keepalive.

Production verification checks the served custom panel checksum, authentication,
all five real account records, private management access, and the public API.
An intentionally paused schedule passes verification.
