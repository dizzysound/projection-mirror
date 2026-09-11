# Architecture

## Threads

A dual-projector session runs about eight threads:

```
main            AppKit UI (or the CLI's main loop)
producer   x1   grab -> crc32 change check -> fit 1024x768 -> colour LUT -> 8 JPEG strips
sender     xN   one per projector: delta -> pace -> AES -> send
keepalive  xN   NOOP on the control channel every ~4s
assoc      xN   UDP-10000 association traffic
```

The frame is encoded **once** and shared. AES runs per projector because each holds a different
session key.

### Why one sender thread per projector

Originally both projectors were driven from one loop. When `.194` began timing out, its blocking
`sendall` stalled `.195` too, and the whole mirror froze. Now the producer publishes the latest
encoded frame into a slot, and each sender consumes it at its own pace with its own delta state.

Verified on hardware: with one projector's cable pulled, the other held a metronomic 6.45 fps
through the entire outage.

## Frame path

1. **Capture** the selected display.
2. **Change detection** — `zlib.crc32` over the raw frame. Unchanged means nothing is sent.
   crc32 replaced md5, which cost 20.8 ms on a 2560×1440 frame — more than resize and encode
   combined. A crc32 collision would skip a single frame, and the keyframe repairs it.
3. **Fit** to the negotiated panel size (1024×768 here), letterboxed or stretched, `BILINEAR`.
4. **Color** — a 256-entry LUT per channel. All sliders at 1.00 hits an identity fast path that
   skips the work entirely.
5. **Encode** the strip column for that panel — 8 × 1024×96 here. Two modes:
   - *Marker-less, quality 50* (default, only hardware-proven path): the scan alone; the projector
     decodes with its built-in q50 tables, so quality is fixed.
   - *Full-JFIF, adaptive quality* (`JFIF=1`, experimental): each strip is a whole JFIF with its own
     tables, so any quality decodes. `adapt_quality()` then trades quality to hold `TARGET_KBPS`,
     which is the byte governor for running higher fps without wedging (see ROADMAP) (see PROTOCOL.md — quality is not adjustable).
6. **Publish** to the shared slot under a lock, bumping a sequence number.

Measured producer cost per frame (Apple Silicon; an older Intel Mac is 2–3× worse):

| Source | fit | build_scans | change hash |
|---|---|---|---|
| 2560×1440 | 14.6 ms | 18.9 ms | 22.7 ms md5 → **0.5 ms crc32** |
| 1024×768 | 0.6 ms | 4.8 ms | 4.8 ms md5 → **0.1 ms crc32** |

Mirroring a **native 1024×768 source is roughly 4× cheaper** than a scaled 2560×1440 one, which is
the strongest argument for the virtual-display item on the roadmap.

## Sender loop

Each sender independently:

- **Strip delta** — sends only the strips whose bytes changed since its own last successful send.
- **Keyframe** — every 3 s it sends all 8 strips, so any band that fell out of step self-corrects.
  This fires *even with no new frame*, which matters precisely on a static screen.
- **Pacing** — the floor is the mode's `min_interval` (0 s in the GUI's free-run, 0.15 s in the
  CLI); the duration of `sendall` is the projector telling you its real drain rate, so the interval
  tracks `max(floor, ema)` of the send duration.
- **Backpressure** — a send timeout is treated as *slow*, not dead: back off and retry, keeping the
  session alive. Abandoning a slow send mid-frame is what hangs the module.
- **Drop and reconnect** — after 3 consecutive hard failures the projector is closed, then
  reacquired with a 30 s settle and 30→120 s escalating backoff. Never a burst; association
  churn is what wedges the module.

`prev` (the delta baseline) advances **only after a successful send**, and every failure path
resets the sequence marker so the frame is retried. Both rules exist because violating them
produced permanently stale bands on the wall.

## Registered projectors

`pm_registry.py` holds the projector list as plain data — `{"ip", "name", "on"}` — with no
AppKit and no sockets, so it is fully testable offline. Until v1.4 the two projectors were
baked in as `LEFT`/`RIGHT` constants and a replaced unit meant a code edit. Because the association
key is now *derived* from a projector's own `602` reply (see CRYPTO.md), any unit on the subnet
works with no code change, and the UI can offer Find / Add / Rename / Remove.

`discover_all()` broadcasts the same `600` probe the wake step already sends and collects every
`602` reply. It is discovery only — it opens no session, so it cannot wedge a projector.

## The wedge (root cause)

The module hangs when it is hammered with *control-plane* traffic - not by data volume, rate, or
quality. Early versions sent a per-frame `NOOP` on TCP-12000 plus a UDP datagram to 12004 after
every frame; that exhausts a limited command budget in the module, which then refuses association
until a physical power cycle. The fix is to match the vendor's control cadence exactly: `NOOP` on a
~4 s timer, no per-frame control, no 12004. (An earlier theory that the module wedged at a fixed
*send* count, and a session-recycling subsystem built to pre-empt it, were disproved and removed -
the budget is on control ops, not sends.) The other way to wedge it is rapid association cycling,
so recovery never re-associates within `RECONNECT_WAIT` (~30 s) of a close, with an escalating
30 -> 120 s backoff on repeated failures.

## Pause

Pause freezes the producer while the sender threads and the `NOOP` keepalive keep running, so the
wall holds its last image and the session stays healthy. Stopping clears the pause, so the next
start is never born paused.

## Modules

`pm_session.py` holds `MirrorSession`, the headless implementation of the above, used by the
CLI harness. `pm_app.py` currently contains its own copy of the same logic. **Unifying the GUI
onto `MirrorSession` is an open roadmap item** — until then the harness tests code that closely
mirrors, but is not identical to, what the app runs.

## Testing

`tests/test_offline.py` needs no projector and no screen-recording permission: protocol framing,
encryption boundaries, key derivation, colour, discovery parsing, and behavioural tests that drive
the real sender worker against a fake sender.

The CLI harness adds hardware-optional load generation:

```bash
pm_cli.py synth both --seconds 60 --static       # the idle-wedge case
pm_cli.py synth both --seconds 60 --gammasweep   # full frames at realistic ~32KB payload
pm_cli.py soak both --minutes 120                # long run with anomaly detection
```

`--gammasweep` reproduces a colour-slider drag: real content re-gamma'd every frame, so every
strip changes and delta degenerates to full frames.
