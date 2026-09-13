#!/usr/bin/env python3
"""
pm_cli.py - command line + test harness for the Projection Mirror.

Built for automation: `synth`, `soak` and `selftest` use GENERATED frames, so they need no
Screen Recording grant and run fine over ssh, and they are deterministic. `mirror` uses the real
screen and does need the grant for whatever launched it.

  ./pm_cli.py state both
  ./pm_cli.py on left ; ./pm_cli.py input both
  ./pm_cli.py probe both              # association test, frees the slot cleanly
  ./pm_cli.py synth both --seconds 60 --static
  ./pm_cli.py mirror left --mon 2 --seconds 120
  ./pm_cli.py soak both --minutes 120
  ./pm_cli.py selftest both
  ./pm_cli.py log -n 40
"""
import argparse, os, subprocess, sys, time, json, importlib, signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_mirror_v2 as fm
import pm_session as fs

VERSION = "1.1"
LEFT, RIGHT = "192.168.1.100", "192.168.1.101"
ALIAS = {"left": [LEFT], "1": [LEFT], "100": [LEFT],
         "right": [RIGHT], "2": [RIGHT], "101": [RIGHT],
         "both": [LEFT, RIGHT], "all": [LEFT, RIGHT]}
APP_LOG = os.path.expanduser("~/Library/Logs/ProjectionMirror.log")


def resolve(tokens):
    if not tokens: return [LEFT, RIGHT]
    out = []
    for t in tokens:
        key = str(t).lower()
        if key in ALIAS: out += ALIAS[key]
        elif key.count(".") == 3: out.append(key)
        else: raise SystemExit("unknown projector: %s (use left/right/both or an IP)" % t)
    seen = set(); return [x for x in out if not (x in seen or seen.add(x))]


def name(ip):
    o = ip.split(".")[-1]
    return ("left/." + o) if ip == LEFT else (("right/." + o) if ip == RIGHT else ip)


def warn_if_gui_running():
    try:
        r = subprocess.run(["pgrep", "-f", "ProjectionMirror"], capture_output=True, text=True)
        if r.stdout.strip():
            print("!! the GUI app is running - it holds the projector's single session slot.")
            print("!! quit it first, or these commands will fight it for the slot.\n")
    except Exception:
        pass


def log(msg): print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------- commands ----------------
def cmd_state(a):
    for ip in resolve(a.projectors):
        st = fm.pj_state(ip)
        print("%-12s power=%-4s input=%-4s mute=%-4s" %
              (name(ip), st["power"], st["input"], st["mute"]))


def _apply(ip, key, want, cmd, label):
    cur = fm.pj_state(ip).get(key)
    if cur is None: print("%-12s %s: no PJLink response" % (name(ip), label)); return
    if cur == want: print("%-12s already %s (%s=%s)" % (name(ip), label, key, cur)); return
    print("%-12s %s: %s=%s -> %s" % (name(ip), label, key, cur, fm.pjlink(ip, cmd)))


def cmd_power(a, on):
    for ip in resolve(a.projectors):
        _apply(ip, "power", "1" if on else "0", "%1POWR " + ("1" if on else "0"),
               "power on" if on else "power off")


def cmd_input(a):
    for ip in resolve(a.projectors):
        _apply(ip, "input", "51", "%1INPT 51", "network input")


def cmd_blank(a, on):
    for ip in resolve(a.projectors):
        cur = fm.pj_is_muted(ip)
        if cur is None: print("%-12s no PJLink response" % name(ip)); continue
        if cur == on: print("%-12s already %s" % (name(ip), "blanked" if on else "unblanked")); continue
        print("%-12s blank %s -> %s" % (name(ip), "on" if on else "off",
                                        fm.pj_blank(ip) if on else fm.pj_unblank(ip)))


def cmd_probe(a):
    warn_if_gui_running()
    bad = 0
    for ip in resolve(a.projectors):
        st = fm.pj_state(ip)
        print("%-12s state %s" % (name(ip), st))
        if st.get("power") != "1":
            # Don't cry "wedged" at a projector that is merely OFF - a powered-down unit resets
            # the TCP-12000 connect even though its network module answers association fine.
            bad += 1
            print("%-12s POWERED OFF (power=%s) - run `on` first, then re-probe"
                  % (name(ip), st.get("power")))
            continue
        try:
            s = fm.Sender(ip); s.connect()
            print("%-12s ASSOCIATE OK (port %s) - freeing slot" % (name(ip), getattr(s, "port", "?")))
            s.close()
        except Exception as e:
            bad += 1
            hint = ("module wedged; needs a physical power cycle"
                    if "501" in str(e) else "check power/input, then retry")
            print("%-12s ASSOCIATE FAILED: %s  <- %s" % (name(ip), e, hint))
    return 1 if bad else 0


def _run(ips, source, seconds, label, sample=15, min_interval=None):
    sess = fs.MirrorSession(ips, source, log=log,
                            min_interval=(fs.MIN_INTERVAL if min_interval is None else min_interval))
    if not sess.start(): return None
    log("%s: running %ss on %s" % (label, seconds, ", ".join(name(i) for i in ips)))
    t0 = time.time(); nxt = t0 + sample
    try:
        while time.time() - t0 < seconds:
            time.sleep(0.3)
            if time.time() >= nxt:
                nxt += sample
                log("  " + "  ".join("%s sent=%d fps=%.1f%s" %
                    (name(ip), v["sent"], v["fps"], "" if v["alive"] else " DOWN")
                    for ip, v in sess.summary().items()))
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        sess.stop()
    return sess.summary()


def cmd_synth(a):
    warn_if_gui_running()
    if a.gammasweep:
        src = fs.gamma_sweep_source(mon=a.mon); label = "GAMMA-SWEEP stress (real content)"
    else:
        src = fs.synthetic_source(motion=not a.static, full=a.fullframe)
        label = "synthetic %s" % ("FULL-FRAME stress" if a.fullframe else
                                  ("STATIC" if a.static else "motion"))
    s = _run(resolve(a.projectors), src, a.seconds, label, min_interval=a.min_interval)
    print(json.dumps(s, indent=2) if s else "session failed")
    return 0 if s else 1


def cmd_mirror(a):
    warn_if_gui_running()
    s = _run(resolve(a.projectors), fs.screen_source(a.mon), a.seconds, "screen mirror")
    print(json.dumps(s, indent=2) if s else "session failed")
    return 0 if s else 1


def cmd_soak(a):
    """Long run with anomaly detection - for the v1 long-term test."""
    warn_if_gui_running()
    if getattr(a, "jfif", False):
        os.environ["JFIF"] = "1"; os.environ["TARGET_KBPS"] = str(a.target_kbps)
        importlib.reload(fm)   # re-read the env flags; fs.fm points at the same module object
        log("JFIF mode ON (full-JFIF strips, adaptive quality); target %.0f KB/s" % a.target_kbps)
    ips = resolve(a.projectors)
    src = fs.synthetic_source(motion=not a.static) if not a.screen else fs.screen_source(a.mon)
    report = os.path.expanduser(a.report)
    sess = fs.MirrorSession(ips, src, log=log,
                            min_interval=(fs.MIN_INTERVAL if a.min_interval is None else a.min_interval),
                            capture=("stream" if getattr(a, "stream", False) else "poll"),
                            display_id=(fm.cg_display_for_mss_monitor(a.mon) or a.mon))
    if not sess.start():
        print("soak: could not start"); return 1
    _mi = sess.min_interval
    log("pacing floor %.3fs (%s)" % (_mi, ("%.1f fps cap" % (1.0/_mi)) if _mi > 0 else "free-run"))
    end = time.time() + a.minutes * 60
    prev = {ip: 0 for ip in ips}
    anomalies = []
    log("SOAK started: %d min, %s" % (a.minutes, ", ".join(name(i) for i in ips)))
    try:
        while time.time() < end:
            time.sleep(a.interval)
            snap = sess.summary()
            line = "  ".join("%s sent=%d fps=%.1f send=%.1fms int=%.3f drops=%d rc=%d%s" %
                             (name(ip), v["sent"], v["fps"], v.get("send_ms", 0),
                              v.get("interval", 0), v["drops"], v["reconnects"],
                              "" if v["alive"] else " DOWN")
                             for ip, v in snap.items())
            if fm.JFIF:
                log("    governor: q=%s frame_bytes=%s" % (getattr(sess, "gov_q", None),
                                                           getattr(sess, "gov_frame_bytes", None)))
            log(line)
            for ip, v in snap.items():
                if v["sent"] == prev[ip] and v["alive"]:
                    anomalies.append("%s STALLED (no frames in %ss) at %s"
                                     % (name(ip), a.interval, time.strftime("%H:%M:%S")))
                    log("  !! %s" % anomalies[-1])
                prev[ip] = v["sent"]
    except KeyboardInterrupt:
        log("soak interrupted")
    finally:
        sess.stop()
    final = sess.summary()
    for ip, v in final.items():
        if v["drops"]: anomalies.append("%s dropped %d time(s)" % (name(ip), v["drops"]))
    with open(report, "w") as fh:
        json.dump({"version": VERSION, "minutes": a.minutes, "ips": ips,
                   "final": final, "anomalies": anomalies,
                   "when": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
    print("\n=== SOAK RESULT ===")
    print(json.dumps(final, indent=2))
    print("anomalies: %s" % (anomalies if anomalies else "none"))
    print("report: %s" % report)
    return 1 if anomalies else 0


def cmd_selftest(a):
    """The 4 checks that were run by hand on 2026-09-08, automated."""
    warn_if_gui_running()
    ips = resolve(a.projectors)
    secs = a.seconds
    results = []

    def check(nm, ok, detail=""):
        results.append((nm, ok, detail))
        print("[%s] %s %s" % ("PASS" if ok else "FAIL", nm, detail))

    print("=== 1. association probe ===")
    ok = True
    for ip in ips:
        try:
            s = fm.Sender(ip); s.connect(); s.close()
        except Exception as e:
            ok = False; print("   %s failed: %s" % (name(ip), e))
    check("association", ok)
    if not ok:
        print("\nprojectors wedged - power cycle before continuing"); return 1

    print("\n(settling %ds so back-to-back associations do not wedge the module)" % int(fs.RECONNECT_WAIT))
    time.sleep(fs.RECONNECT_WAIT)
    print("\n=== 2. STATIC hold (the idle-wedge case) ===")
    s = _run(ips, fs.synthetic_source(motion=False), secs, "static")
    ok = bool(s) and all(v["errors"] == 0 and v["drops"] == 0 for v in s.values())
    check("static hold %ss (no wedge)" % secs, ok, json.dumps(s) if s else "")

    print("\n(settling %ds before the next association)" % int(fs.RECONNECT_WAIT))
    time.sleep(fs.RECONNECT_WAIT)
    print("\n=== 3. MOTION ===")
    s = _run(ips, fs.synthetic_source(motion=True), secs, "motion")
    ok = bool(s) and all(v["drops"] == 0 and v["fps"] >= a.min_fps for v in s.values())
    check("motion >= %.1f fps" % a.min_fps, ok, json.dumps(s) if s else "")

    if len(ips) > 1 and s:
        spread = max(v["fps"] for v in s.values()) - min(v["fps"] for v in s.values())
        check("dual balance (fps spread < 1.5)", spread < 1.5, "spread=%.2f" % spread)

    bad = [n for n, ok, _ in results if not ok]
    print("\n=== SELFTEST %s ===" % ("PASSED" if not bad else "FAILED: " + ", ".join(bad)))
    return 1 if bad else 0


def cmd_log(a):
    if not os.path.exists(APP_LOG): print("no log at %s" % APP_LOG); return 1
    subprocess.run(["tail", "-n", str(a.n)] + (["-f"] if a.follow else []), check=False)
    return 0


def cmd_discover(a):
    """Broadcast the 600 sweep and print every 602 reply. Discovery only - it opens no session,
    so it is safe to run at any time, including against a projector that is already mirroring."""
    found = fm.discover_all(timeout=a.seconds)
    if not found:
        print("no projectors answered (powered on? same subnet?)"); return 1
    for i in found:
        print("%-15s  %-8s  location=%-16r  key=%s"
              % (i["ip"], i.get("name", ""), (i.get("location") or "").strip(),
                 fm.pkey_for(i["ip"]).hex()))
    return 0


def _on_sigterm(*_):
    raise KeyboardInterrupt          # routes into _run's finally -> sess.stop() (graceful QUIT/182)

def main():
    try: signal.signal(signal.SIGTERM, _on_sigterm)   # `kill <pid>` closes the session cleanly
    except (ValueError, OSError): pass                # not the main thread / unsupported
    p = argparse.ArgumentParser(description="Projection Mirror CLI / test harness")
    p.add_argument("--version", action="version", version="f300u-cli %s" % VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def proj(sp): sp.add_argument("projectors", nargs="*", help="left|right|both or IP")

    for nm, fn in (("state", cmd_state), ("input", cmd_input)):
        sp = sub.add_parser(nm); proj(sp); sp.set_defaults(fn=fn)
    sp = sub.add_parser("on");  proj(sp); sp.set_defaults(fn=lambda a: cmd_power(a, True))
    sp = sub.add_parser("off"); proj(sp); sp.set_defaults(fn=lambda a: cmd_power(a, False))
    sp = sub.add_parser("blank");   proj(sp); sp.set_defaults(fn=lambda a: cmd_blank(a, True))
    sp = sub.add_parser("unblank"); proj(sp); sp.set_defaults(fn=lambda a: cmd_blank(a, False))
    sp = sub.add_parser("probe"); proj(sp); sp.set_defaults(fn=cmd_probe)
    sp = sub.add_parser("discover", help="find every projector on the subnet (safe: no session)")
    sp.add_argument("--seconds", type=float, default=3.5)
    sp.set_defaults(fn=cmd_discover)

    sp = sub.add_parser("synth", help="send generated frames (no Screen Recording needed)")
    proj(sp); sp.add_argument("--seconds", type=int, default=60)
    sp.add_argument("--static", action="store_true", help="unchanging frames = the idle wedge case")
    sp.add_argument("--fullframe", action="store_true",
                    help="repaint everything: all 8 strips every frame (~23KB)")
    sp.add_argument("--gammasweep", action="store_true",
                    help="real screen content, gamma shifted each frame = the ACTUAL v1.0 wedge load")
    sp.add_argument("--mon", type=int, default=1)
    sp.add_argument("--min-interval", dest="min_interval", type=float, default=None,
                    help="override the per-projector pacing floor (default 0.15 = 6.7fps cap)")
    sp.set_defaults(fn=cmd_synth)

    sp = sub.add_parser("mirror", help="mirror the real screen (needs Screen Recording)")
    proj(sp); sp.add_argument("--seconds", type=int, default=60)
    sp.add_argument("--mon", type=int, default=1)
    sp.set_defaults(fn=cmd_mirror)

    sp = sub.add_parser("soak", help="long run with anomaly detection")
    proj(sp); sp.add_argument("--minutes", type=int, default=60)
    sp.add_argument("--interval", type=int, default=30)
    sp.add_argument("--static", action="store_true")
    sp.add_argument("--screen", action="store_true", help="use real screen instead of synthetic")
    sp.add_argument("--mon", type=int, default=1)
    sp.add_argument("--report", default="~/Desktop/f300u-soak-report.json")
    sp.add_argument("--min-interval", dest="min_interval", type=float, default=None,
                    help="pacing floor override; 0.11 ~= 9fps, 0.15 ~= 6.7fps")
    sp.add_argument("--jfif", action="store_true", help="EXPERIMENTAL: full-JFIF strips + adaptive quality")
    sp.add_argument("--stream", action="store_true", help="V2e: CGDisplayStream push capture (needs --screen)")
    sp.add_argument("--target-kbps", dest="target_kbps", type=float, default=0.0,
                    help="JFIF only: hold this sent-byte rate by trading quality")
    sp.set_defaults(fn=cmd_soak)

    sp = sub.add_parser("selftest", help="automated version of the hardware verification")
    proj(sp); sp.add_argument("--seconds", type=int, default=45)
    sp.add_argument("--min-fps", dest="min_fps", type=float, default=4.0)
    sp.set_defaults(fn=cmd_selftest)

    sp = sub.add_parser("log"); sp.add_argument("-n", type=int, default=30)
    sp.add_argument("-f", "--follow", action="store_true"); sp.set_defaults(fn=cmd_log)

    a = p.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
