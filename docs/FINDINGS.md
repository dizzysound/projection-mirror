# Findings

Non-obvious things learned the hard way, including the theories that turned out to be wrong.
They are recorded because each one cost real time, and two of them cost ladder trips to
ceiling-mounted projectors.

## What actually wedges a projector

**Rapid association cycling — not data volume.**

| Experiment | Result |
|---|---|
| 202 KB/s of full frames, dual, 60–75 s | Clean. Zero errors, twice. |
| Four sessions opened ~4 s apart | Wedged immediately |

The consequence is uncomfortable: **the auto-reconnect logic was itself bricking the projectors.**
On a stall, early versions dropped the projector and retried association every ~8 s — exactly the
hammering pattern that turns a recoverable stall into a hard wedge needing a physical power cycle.
Hence the 30 s settle and 30→120 s escalating backoff.

## The idle wedge

With send-on-change video, a static screen sends no frames. Early versions only sent `NOOP` after a
frame, so an idle screen meant an idle control channel, a session timeout, and a wedged module.
The real Wireless Manager sends `NOOP` every ~4 s unconditionally. Adding an independent keepalive
fixed it: verified with a 58-second static screen, no wedge.

This is also why a blank capture was so destructive — no permission meant an unchanging frame,
which meant no frames sent, which wedged the projector.

## JPEG quality cannot be adjusted

Codec 3 transmits only the entropy-coded scan; the quantization tables are stripped and the
projector decodes with its own fixed set. Encoding at any quality other than 50 misscales the
coefficients and shows up as a **brightness shift**, not a quality change. The "Quality" slider was
removed for this reason. Frame rate and resolution are the only real bandwidth levers.

## A denied screen capture looks like a successful one

macOS returns a correctly sized image containing only the desktop wallpaper. Any self-test based on
image dimensions passes while you mirror nothing. Test whether other applications' **window titles**
are visible instead — macOS gates those behind the same permission.

## The projector wedges on cumulative BYTES, and lowering the frame rate does not save it

Sustained heavy motion wedges the network module regardless of frame rate. Measured with a
synthetic source that redraws the whole frame every frame:

| pacing floor | fps | frames to wedge | wall time |
|---|---|---|---|
| 0.08 s | 11.8 | 949 | 1m20s |
| 0.11 s | 8.7 | 3563 | 7m05s |
| 0.15 s | 6.3 | 3239 | 8m45s |
| static | 0.33 | survived (300) | 15m00s clean |

0.11 and 0.15 both die near ~3,200–3,600 frames, so **frame rate is not the lever.** A slower rate
only postpones the cliff slightly.

The real unit is **bytes / changed strips, not frames.** The same 0.15 rate that died at 3,239
frames here survived ~6,500 frames on a different day — because that run mirrored a mostly-static
real screen (tiny per-frame deltas) while this one changed every strip of every frame. A static
screen (keyframe only) survives indefinitely. So each frame's cost scales with how much of it
changed, and the module wedges on accumulated bytes.

**What this means in practice.** A slide-driven service is mostly static with occasional changes —
a light load that survives. A long, continuous video clip is the torture case. The synthetic
"motion" source used for these tests is harsher than almost any real content, so its failure does
not condemn normal use — but sustained video will eventually wedge a projector at any frame rate.

**Superseded (2026-09-09).** The "accumulated bytes / cumulative-byte budget" reading above was
DISPROVED. The module does not wedge on bytes or frame rate - it wedges on per-frame CONTROL
traffic: a `NOOP` plus a UDP-12004 datagram this project used to send after *every* frame, which
exhausts a command budget in the network module. More frames per second meant more control ops per
second, which is why a higher frame rate looked like it wedged "faster." The fix is to match the
vendor's control cadence exactly (`NOOP` ~4 s, no per-frame control, no 12004); the
session-recycling subsystem once built for the byte theory has been removed. A separate wedge
trigger is rapid association cycling, so recovery keeps a ~30 s settle (with an escalating
30 -> 120 s backoff on repeated failures).

## Frame rate has a hard sustained ceiling, and short tests cannot find it

Roughly **6.5 fps per projector** (a 0.15 s pacing floor) is the proven safe rate. Raising it to a
0.08 s floor produced ~11.8 fps, which the projector accepted for about **90 seconds and 949
frames** — and then it stopped accepting anything at all, hung, and needed a physical power cycle.

Two things make this dangerous:

- **The damage is cumulative.** Sixty- to seventy-five-second stress runs at the same payload
  passed cleanly. Only a multi-minute soak exposed it.
- **Backoff cannot rescue it.** Adaptive pacing engaged correctly and stepped 0.16 → 3.0 s, but a
  module that is already saturated does not recover.

It also invalidates a conclusion drawn earlier on this page: "byte-rate overrun is disproved" was
measured with the pacing floor pinned at 0.15 s. It said nothing about 11.8 fps. **Do not raise
`MIN_INTERVAL` without a soak of several minutes.**

## Theories that were wrong

Recorded deliberately; each was asserted with more confidence than the evidence supported.

| Theory | Reality |
|---|---|
| "The slowness is screen capture" | It was sequential sends — a timing-out projector blocked the healthy one |
| "The permission churn is code signing" | It was our own `tccutil reset`, firing 4 ms after an async permission request and deleting the entry macOS had just created |
| "The wedge is byte-rate overrun" | 202 KB/s dual ran clean. The trigger is association cycling |
| "Parallelize the 8 strip encodes — an easy win" | Encoding takes 1.74 ms total, and threading it is *slower*. The 25 ms figure was never measured |
| "6.4 fps, so pacing is the hardware limit" | The 6.7 fps cap had been added by hand — but raising it later wedged a projector, so the cap was right for the wrong reason |
| "9–10 fps is achievable, the cap is costing frame rate" | 11.8 fps wedged a projector in 90 seconds. 6.5 fps is the sustainable rate |

The pattern in every case: a plausible mechanism was adopted without measurement, and the
measurement — when finally taken — contradicted it.

## Testing blind spots

Four hardware tests passed and the build was called solid. Three later regressions were invisible
to all of them, because **every hardware test used changing content**:

- unsent strips recorded as delivered, leaving permanently stale bands
- a failed send never retried on a static screen
- the periodic keyframe never firing on a static screen — exactly when it was needed

All three were found by reading the code, not by running it, and are now covered by
`tests/test_offline.py`. Static-screen behavior deserves as much test attention as motion.

## Smaller things

- Screen Recording lives in the **system** TCC database, needing root to read; the user database is
  empty for it, which proves nothing.
- A PyInstaller `--windowed` app that never opens a Cocoa window bounces in the Dock forever;
  either give it a real window or set `LSUIElement`.
- Per-projector keys are constant and survive power cycles — only the session key changes.
