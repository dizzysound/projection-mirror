# Remote control API (V2f) — Stream Deck / Bitfocus Companion

Enable it in the app: tick **Remote control API (Stream Deck / Companion)**. The status log prints
the URL, e.g. `http://192.168.1.50:8765`. LAN-only, no auth — do not expose to the internet.

Every request returns JSON `{ok, path, status}`; `status` is the full current state, so Companion
button feedback stays in sync. GET or POST both work (a plain web-request button is fine).

| Route | Effect |
|---|---|
| `GET /status` | full state: mirroring, paused, mode, quality, subsampling, virtual_display, projectors, sent |
| `/start` `/stop` | start / stop mirroring (uses the current projector + display selection) |
| `/pause` `/resume` | freeze / resume the wall (session stays open) |
| `/mode?m=fast` or `m=hq` | picture mode |
| `/vdisplay?on=1` or `on=0` | virtual 1024×768 display |
| `/blank?on=1` or `on=0` | projector AV-mute (PJLink) |
| `/power?on=1` or `on=0` | projector power (PJLink) |
| `/input` | switch projectors to NETWORK input (PJLink) |

## Bitfocus Companion
- Mirror actions: **Generic HTTP** module → GET `http://<mac-ip>:8765/<route>`.
- Projector power/input/AV-mute can ALSO go direct via Companion's **PJLink** module (TCP 4352,
  password `panasonic`) without the app — either works.
- For button feedback, poll `GET /status` and key off the JSON fields.
