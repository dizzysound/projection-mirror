# Operations

## Example projector map

| Projector | IP | Location field | Role |
|---|---|---|---|
| Projector 1 | 192.168.1.100 | `LEFT` | example |
| Projector 2 | 192.168.1.101 | `RIGHT` | example |

The presentation Mac is an Intel iMac on macOS 12.7.6 running Python 3.9. Present on a
**1024×768 second display** and mirror that: it matches the panel exactly, so nothing is scaled or
letterboxed, the audience sees only the slides, and the producer cost drops about 4×.

## Running

Launch the app, tick the projectors, choose the source display, press **Start**. Pressing **Start**
again while running reconciles the selection in place — it adds newly ticked projectors and retires
unticked ones without disturbing the ones already streaming.

Picture sliders persist across launches in
`~/Library/Application Support/ProjectionMirror/settings.json`.

## Screen Recording permission

The app requests it before the UI appears, and **holds its main window back for 6 seconds** so the
system prompt is not buried behind it. Once granted, the app **relaunches itself** — macOS will not
apply a new screen-recording grant to an already-running process.

If capture looks wrong, check the log for:

```
capture capability (other apps' window titles visible) = True
Capture verified: real screen content is visible.
```

That test exists because **a denied screen capture still returns a correctly sized image** — just
the desktop wallpaper with no windows. Any check based on image size will happily pass while you
mirror nothing but the wallpaper. macOS hides other applications' window titles without the grant,
so title visibility is the honest test.

The app is signed with a stable self-signed certificate, so the grant **survives rebuilds**. Never
run `tccutil reset` immediately after requesting permission: `CGRequestScreenCaptureAccess` returns
the *current* status immediately and raises its prompt asynchronously, so a `False` return does not
mean "no prompt appeared" — resetting there deletes the entry macOS just created.

## Recovering a wedged projector

Symptoms: PJLink still answers on 4352, but association fails with `no 501 response`, and a
teardown logs `QUIT -> (no reply)` instead of `QUIT -> 182 OK`.

```bash
pm_cli.py state both     # is it just powered off? power=0 is not "wedged"
pm_cli.py probe both     # association test; frees the slot cleanly if healthy
```

**A wedged network module is cleared only by a physical power cycle.** A PJLink power cycle does
not work, because the module stays powered in standby. Toggling the network cable does not work
either.

To avoid wedging one in the first place: **leave 20–30 seconds between sessions**. Rapid
association cycling is the trigger — not throughput. A test script that opened four sessions four
seconds apart wedged a projector immediately, while 202 KB/s to both projectors at once ran clean.

## Build and deploy

```bash
./src/build.sh          # builds, signs with the stable identity, deploys to ~/Desktop
```

Signing uses a dedicated keychain (`f300u-signing.keychain`) holding a self-signed code-signing
certificate. Re-signing every build with the same certificate keeps a constant designated
requirement, which is what preserves the TCC grant. Gatekeeper does not trust a self-signed
certificate, so the build strips the quarantine attribute; that is fine for a locally built,
locally run app.

**Run `tests/test_offline.py` before every build.**

## Logs

`~/Library/Logs/ProjectionMirror.log` — 2 MB × 6 rotating, and it captures the engine's own
stdout, which a windowed `.app` bundle otherwise discards. It records each projector's PJLink
state before connecting, association keys, data ports, throughput samples, and every send error.

Read the log before forming a theory. Several confident diagnoses during development were wrong,
and the log settled each one.

## PJLink notes

- Password is the F300U default, `panasonic`.
- Commands read current state before acting, so the log shows `already on (power=1)` rather than
  firing blindly. This matters on a flaky unit, where a blind command tells you nothing.
- `%1POWR ?` returns `0` off, `1` on, `2` cooling, `3` warming. During warm-up the reported input
  can be stale — pressing **Network In** then is harmless and corrects it.
