# Changelog

## v2.5
- **PJLink power is a state machine, not a boolean.** Starting a mirror while a projector was
  cooling down mirrored to a projector that stayed switched off. Cooling reports `%1POWR=2`,
  refuses `%1POWR 1` with `ERR3` ("unavailable time", spec v1.04 s4.1), and then settles at
  `0` STANDBY — never at `1`. `power_on_network()` fired one power-on into that `ERR3` window,
  discarded the error, then waited 100 s for a `1` that cannot arrive and gave up silently
  (measured: 132 s, projector still off). It now waits each transition out, powers on from
  standby, bounds the whole wait, and returns success so `_connect_with_retry` can say when the
  projector never came up.
- **PJLink responses are read as whole CR-terminated lines.** Both `pjlink()` and `pjctl.send()`
  took whatever one `recv()` returned. A greeting split across TCP segments yielded a truncated
  auth seed, so the MD5 digest was wrong and the command went out unauthenticated — reproduced:
  the client read one byte of `PJLINK 1 <seed>` and sent no digest at all. Sockets now close on
  every path, including the error path, which also closes an fd leak in both.
- **`pjctl` explains PJLink error codes** instead of printing a bare `%1POWR=ERR3`, and skips a
  power command during cooling/warm-up with a note saying why, rather than firing one that the
  projector is required to reject.
- Offline suite grown by 5 PJLink checks covering the cooling path, standby power-on, and
  segmented-greeting authentication. They run against a mock projector on loopback — no hardware.


## v2.4
- **GUI unified onto `MirrorSession`.** `pm_app.py` no longer carries its own copy of the
  producer loop and per-projector sender (was ~212 duplicated lines). `_mirror()` now drives the
  same `pm_session.MirrorSession` the CLI/soak harness uses, so every engine change lands once.
  Status, pause, live projector add/remove, mode switch, and the remote-control API all read/drive
  the session. Behavior is unchanged; this removes the drift risk between the two send paths.
- Built universal2 (x86_64 + arm64), signed with the stable self-signed identity. Test apps only
  (`ProjectionMirror-universal2.app` on the M1 and the test Mac); production stays v1.6 until a
  service-length hardware run validates the GUI.

## v1.5
- **Registered projector list** (`pm_registry.py`) replaces the baked `LEFT`/`RIGHT` constants.
  Add, rename, remove, and select projectors from the UI; pre-v1.5 settings migrate automatically.
- **Find Projectors**: `discover_all()` sweeps the subnet for `602` replies. Discovery only, so it
  cannot wedge a unit. Each discovered projector's key is derived, not configured.
- **Pause**: freezes the producer while senders and the keepalive keep the session alive.
- Offline suite grown to 78 checks.

## v1.4
- Reconnect backoff: 30 s settle after a drop, then 30→120 s escalating, one attempt per round.
  Rapid association cycling is what wedges a projector, so recovery must never hammer it.
- Pacing floor 0.15 s → 0.08 s, removing a hand-added 6.7 fps cap that existed for a disproved
  throughput theory. Adaptive pacing remains the real guard.

## v1.3
- Advance the delta baseline **only after a successful send**. Previously a failed send marked its
  strips as delivered, leaving permanently stale bands (visible as tearing).
- Periodic keyframe: all 8 strips every 3 s, so any band out of step self-corrects.

## v1.2
- Gamma slider range narrowed to 0.85–1.20 and labeled in **effective gamma** (2.59 … 1.83). The
  old 0.50–1.50 range spanned effective 4.40–1.47, nearly all of it unusable.

## v1.1
- Adaptive pacing from measured `sendall` duration; a send timeout is treated as backpressure, not
  death.
- Colour changes debounced 250 ms, so a slider drag no longer causes dozens of full-frame
  invalidations.
- Video socket timeout 4 s → 12 s. Abandoning a merely slow send mid-frame is what hangs the module.

## v1.0
First release. Verified on hardware:
- Static screen 58 s with no wedge (4 s `NOOP` control keepalive)
- 6.3 fps single, **6.40 / 6.41 fps dual simultaneously** with no mutual interference
- Auto-reconnect after a cable pull: ~22 s, healthy projector held 6.45 fps throughout
- Clean teardown (`QUIT -> 182 OK`)

Also in v1.0: native AppKit GUI, per-projector independent sender threads, state-aware PJLink
control, persistent picture settings, stable code signing so the Screen Recording grant survives
rebuilds, and a rotating debug log capturing engine output.

## Unreleased
- Fast mode is now free-run (no fps floor) with quality 15-100. The fps cap only ever existed for
  the wedge, which was our per-send control traffic, not frame rate; the EMA pacing and TCP
  backpressure self-limit to the module's real drain rate. Governor byte budget uses a 0.1s nominal
  cadence so a zero floor still computes a per-frame target. HQ unchanged (q85, 4 fps).
- Input-guide OSD fixed. The projector shows its NETWORK input guide by default; a UDP-10000 beacon
  dismisses it for a few seconds. The beacon must be rebuilt per session and encrypted with the live
  session key (session-keyed beacon): `7000430`+token + AES-ECB(m_CommKey, user.ljust(32)) +
  status. Sent every 2 s to beat the redraw window (10 s left a visible toggle; a static replayed
  beacon never validated). 55-byte UDP presence packets, unrelated to the per-send wedge budget.
- Segmented-control helper text moved to a tooltip (was wrapping).
- Beacon/keyframe debug logging added.
- Test files are now layout-agnostic (run from the flat vault copy or the repo `src/` layout).
- 45-min dual-projector YouTube run on the fixed control plane: 17,300 sends each, zero anomalies.
- Beacon: after association send only the `700` beacon every ~10 s. The periodic `500` (an
  association request) re-selected the input every period - input OSD popping, picture-mode menu
  greying out.
- Picture sliders removed (the projector's own menu handles picture quality). Replaced by a mode
  toggle: **HQ (slow)** = full-JFIF q85, 4 fps; **Fast (variable)** = adaptive q15-70 against a
  byte target, 9 fps. Live switch; persisted in settings.
- Crash on quit fixed: a Python error inside a main-queue UI block became an uncaught ObjC
  exception (SIGABRT). `_on_main` now logs instead of throwing and drops blocks once terminating.
- **Root cause of the module wedge found and fixed (v1.6).** Early versions sent a `NOOP` and a UDP
  datagram to port 12004 after every frame, and a 100 ms 700/500/600 association blast all session.
  No vendor app (Mac, Windows, Android) sends any of that. The projector has a ~3300-control-command
  budget per power-on; we spent it in ~7 min of motion. The engine now sends the vendor-exact
  control plane (NOOP every ~4 s, no 12004, beacon burst then 10 s cadence): 9,316 sends / 25 min
  clean on hardware, versus a ~3,560 ceiling before. `CTL_LEGACY=1` keeps the old traffic for A/B.
- Session recycling removed (unnecessary; each recycle is an association cycle).
- "Remote Control (browser)" button removed: `remocon.cgi` is not supported on the F300NT family.
- PROTOCOL.md §4 corrected (the 12004 "render trigger" was never observed in any capture).
- **GUI recycling (v1.6):** the AppKit app's worker now carries the same proactive session-recycle
  logic as MirrorSession (identical trigger/reset, verified by diff), so the shipped GUI survives
  the per-send wedge budget too. Still unverified on hardware; GUI<->MirrorSession unification
  remains the roadmap cleanup.
- **Reverted the v1.4 pacing change.** `MIN_INTERVAL` is back to 0.15 s (~6.5 fps). The 0.08 s
  floor ran at 11.8 fps and wedged a projector after ~90 s and 949 frames; the failure is
  cumulative, so short stress tests could not see it. Marked in the source as a proven ceiling.

## Verified on hardware 2026-09-09
- Rebuild clean; the Screen Recording grant **survived the rebuild** (`preflight=True` on a fresh
  binary), confirming the stable signing identity
- 92 offline checks pass on the target Mac under Python 3.9
- **Resolution negotiation confirmed live** — both projectors reported `1024x768` via `STAT RES`
  and the client adopted it

## Earlier offline work (now built)
- **Resolution negotiation.** The projector's real panel size is read from the `STAT RES` reply
  (`112 <w> <h>`) and drives the scale target, strip layout, and the panel size in every strip
  header. Non-multiple heights get a shorter final strip. The 1024×768 path is byte-identical on
  the wire to the previous encoder.
- `pm_pkey.py`: pure-Python key derivation; keys are now **derived** from discovery rather than
  captured, so a new projector needs no manual capture
- Change detection md5 → crc32 (43× faster; 20.8 ms → 0.5 ms on a 2560×1440 frame)
- Keyframe now fires on a static screen, and a failed send is retried — both were broken by a gate
  on "has a new frame arrived"
- `tests/test_offline.py`: 92 checks requiring no hardware
- Gamma calibration target
