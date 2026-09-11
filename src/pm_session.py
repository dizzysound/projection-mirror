#!/usr/bin/env python3
"""
pm_session.py - headless mirror session engine (v2 shared core).

Same architecture the v1.0 GUI proved on hardware: ONE producer thread encodes frames, and each
projector has its OWN sender thread with pacing, strip-delta, drop-after-3-failures and
auto-reconnect - so a dead projector can never stall a healthy one.

v1.0 duplicated this inside pm_app.py. This module exists so the CLI harness and the v2 GUI
run the SAME code path, which is the point of a test harness.
"""
import threading, time, hashlib, zlib
import pm_mirror_v2 as fm

MIN_INTERVAL = 0.15          # see pm_app.py: 0.08 wedged .194 on 2026-09-09 after ~90s
FAIL_LIMIT   = 3             # consecutive send failures before dropping a projector
RECONNECT_WAIT = 30      # settle after a close before re-associating - rapid cycling WEDGES the module
RECONNECT_BACKOFF0 = 30.0    # first gap on a FAILED reconnect, then doubling ...
RECONNECT_BACKOFF_MAX = 120.0  # ... capped here
KEYFRAME_SEC = 3.0       # periodic full-frame refresh so stale bands self-correct


def screen_source(mon=1):
    """Live screen capture. Needs Screen Recording for the launching process."""
    return lambda: fm.grab_screen(mon=mon)


def synthetic_source(motion=True, size=(1024, 768), full=False):
    """Generated frames - no Screen Recording needed, and deterministic.
    motion=False reproduces the STATIC-screen case that used to wedge the projector.
    full=True repaints the WHOLE frame every time, so all 8 strips differ and strip-delta
    degenerates to full frames - the exact load a colour-slider drag produces, which wedged
    both projectors on 2026-09-08. This is the worst case the pacing must survive."""
    from PIL import Image, ImageDraw
    t0 = time.time()

    def make():
        img = Image.new("RGB", size, (16, 16, 24))
        d = ImageDraw.Draw(img)
        if full:
            ph = int((time.time() - t0) * 140) % 256
            for by in range(0, size[1], 24):
                d.rectangle([0, by, size[0], by + 24],
                            fill=((by * 3 + ph) % 256, (by * 5 + ph * 2) % 256,
                                  (by * 7 + ph * 3) % 256))
            d.text((30, 30), "FULL-FRAME STRESS", fill=(255, 255, 255))
            return img
        d.rectangle([0, 0, size[0] - 1, size[1] - 1], outline=(90, 90, 110), width=3)
        if motion:
            el = time.time() - t0
            x = int((el * 220) % max(1, (size[0] - 160)))
            y = int(size[1] / 2 + 180 * __import__("math").sin(el * 1.7))
            d.rectangle([x, y, x + 150, y + 90], fill=(220, 90, 40))
            d.text((30, 30), "MOTION  t=%.1fs" % el, fill=(240, 240, 240))
        else:
            d.text((30, 30), "STATIC TEST FRAME", fill=(240, 240, 240))
            d.rectangle([420, 330, 610, 440], fill=(40, 120, 200))
        return img
    return make


def gamma_sweep_source(mon=1, base=None):
    """FAITHFUL reproduction of the v1.0 wedge: real screen content re-gamma'd every frame, so
    every strip re-encodes (full frames) at a REALISTIC payload (~32KB), which is exactly what
    dragging the Gamma slider produced. Synthetic gradients are only ~23KB and understate it."""
    import math
    from PIL import Image
    b = base if base is not None else fm.grab_screen(mon=mon)
    if b is None:
        raise RuntimeError("gamma sweep needs a screen grab (Screen Recording) or a base image")
    b = b.convert("RGB")
    t0 = time.time()

    def make():
        g = 0.80 + 0.40 * (0.5 + 0.5 * math.sin((time.time() - t0) * 1.5))   # 0.80 .. 1.20
        lut = bytes(min(255, max(0, int(((i / 255.0) ** (1.0 / g)) * 255.0 + 0.5)))
                    for i in range(256))
        return Image.merge("RGB", tuple(ch.point(lut) for ch in b.split()))
    return make


class MirrorSession:
    # Class-level defaults for every field the producer and governor touch. Tests (and any partial
    # construction) build sessions with bare __new__, which skips __init__ entirely; without these a
    # new field silently turns into an AttributeError swallowed by the producer's except.
    _mode_dirty = False
    _prod_ema = None
    _prod_last = None
    _gov_reencodes = 0
    _content_fps = None          # from the capture source's seq counter - the EXOGENOUS rate
    _cf_t0 = None
    _cf_s0 = 0
    governor = False
    target_kbps = 0.0

    def __init__(self, ips, frame_source, log=print, min_interval=MIN_INTERVAL,
                 letterbox=True, auto_reconnect=True, capture="poll", display_id=1):
        fm.FIT = "letterbox" if letterbox else "stretch"
        self.letterbox = letterbox
        self.ips = list(ips)
        self.src = frame_source
        self.log = log
        self.min_interval = min_interval
        self.auto_reconnect = auto_reconnect
        self.capture = capture          # "poll" (mss/synthetic via frame_source) or "stream" (CGDisplayStream)
        self.display_id = display_id
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()   # set == paused (producer frozen, senders keep going)
        self._slock = threading.Lock()
        self._state = {"scans": None, "seq": 0}
        self.current, self.wanted, self.alive = {}, {}, {}
        self.stats = {}          # ip -> counters
        self.gov_q = None; self.gov_frame_bytes = None
        # live picture-mode state (GUI drives these via set_mode(); CLI leaves them at defaults
        # and the fm.JFIF/TARGET_KBPS env path below still works unchanged).
        self.governor = False        # True: run the adaptive-quality byte governor in _produce
        self.target_kbps = 0.0       # >0: governor target (else falls back to fm.TARGET_KBPS)
        self._gov_nominal = 0.1      # legacy nominal cadence (kept for compat; see _gov_target_bytes)
        self._prod_ema = None        # EMA of the interval between REAL content changes (seconds)
        self._prod_last = None
        self._gov_reencodes = 0      # consecutive governor-driven re-encodes of the same frame
        self._mode_dirty = False     # force one re-encode after a live mode change
        self._workers = []
        self._last_close = {}        # ip -> time of its last Sender close (settle clock)
        self._producer = None
        self._producer_crashed = False
        self._reporter = None
        self.report_sec = 0.0        # >0: log a periodic throughput line (GUI sets 30; CLI leaves off)
        self.started_at = None

    GOV_MAX_REENCODE = 6         # bound the converge-now loop so it cannot spin on one frame

    def _gov_target_bytes(self):
        """Per-frame byte budget for the adaptive governor.

        The budget is a BYTE RATE divided by the cadence we actually put frames on the wire - not a
        fixed per-frame number. The old code used KBPS*1024*max(min_interval, 0.1), i.e. it assumed
        a nominal 10fps forever. On a static slide we send one frame every KEYFRAME_SEC (3s) and use
        roughly 30x less bandwidth than the target allows, yet the governor still clawed quality
        down to fit a 10fps-sized frame - Chris saw q=20 on slides that should have been q=100.

        The floor on the cadence is the sender's keyframe: even with no content change at all, a
        full frame goes out every KEYFRAME_SEC. At 10fps this returns exactly the old 70kB, so
        motion behaviour is unchanged.

        The rate MUST be exogenous - set by the content, never by anything we choose. The send
        rate (6292f48, reverted) and even our own encode cadence both depend on quality through
        encode cost, and a denominator that depends on the quantity being controlled is a
        feedback loop. The capture source's seq counter is the clean signal.
        """
        tkbps = self.target_kbps if self.target_kbps > 0 else fm.TARGET_KBPS
        if tkbps <= 0:
            return 0.0
        if self._content_fps is not None:          # stream capture: the source's seq counter
            fps = self._content_fps
        else:                                      # poll capture: cadence of crc-detected changes
            ema = self._prod_ema
            fps = (1.0 / ema) if (ema and ema > 0) else 0.0
        eff_rate = max(fps, 1.0 / KEYFRAME_SEC)
        return min(float(fm.VENDOR_FRAME_CAP), tkbps * 1024.0 / eff_rate)

    # ---------- lifecycle ----------
    def start(self):
        senders = []
        for ip in self.ips:
            st = fm.pj_state(ip)
            self.log("%s pre-connect state: %s" % (ip, st))
            self.stats[ip] = {"sent": 0, "errors": 0, "drops": 0, "reconnects": 0}
            self.wanted[ip] = True
            try:
                s = fm._connect_with_retry(ip, tries=1)   # one attempt; the worker retries with a settle
            except Exception as e:
                s = None; self.log("%s connect error: %s" % (ip, e))
            if s:
                senders.append(s); self.current[ip] = s; self.alive[ip] = True
                self.log("%s connected (key %s)" % (ip, s.commkey.hex()[:8]))
                try:
                    if fm.pj_is_muted(ip): fm.pj_unblank(ip)
                except Exception: pass
            else:
                self.current[ip] = None; self.alive[ip] = False
                self.log("%s FAILED to connect" % ip)
        if not any(self.alive.values()):
            self.log("no projectors connected"); return False
        pw, ph = fm.adopt_panel(senders, log=self.log)      # encode for what they actually report
        self.log("encoding for %dx%d" % (pw, ph))
        self.started_at = time.time()
        for ip in self.ips:
            t = threading.Thread(target=self._worker, args=(ip, self.current.get(ip)),
                                 name="tx-%s" % ip.split(".")[-1], daemon=True)
            self._workers.append(t); t.start()
        self._producer = threading.Thread(target=self._produce, name="encode", daemon=True)
        self._producer.start()
        if self.report_sec > 0:
            self._reporter = threading.Thread(target=self._report_loop, name="report", daemon=True)
            self._reporter.start()
        return True

    # ---------- live picture mode ----------
    def set_mode(self, min_interval=None, governor=None, target_kbps=None):
        """Change pacing/governor live. The producer re-encodes the current frame once (even on
        a static screen) so the change takes effect immediately; the sender re-reads min_interval
        on its next send via the existing max(min_interval, ema*1.25)."""
        if min_interval is not None: self.min_interval = min_interval
        if governor is not None: self.governor = governor
        if target_kbps is not None: self.target_kbps = target_kbps
        self._mode_dirty = True

    def set_wanted(self, ips):
        """Live add/remove projectors while running. Deselected -> wanted False (its worker
        exits and closes its socket). Newly selected -> connect on the worker's own reconnect
        path and spawn a worker if none is alive for it."""
        ips = list(ips)
        for ip in list(self.wanted):
            self.wanted[ip] = ip in ips
        for ip in [p for p in list(self.ips) if p not in ips]:   # removed -> drop from status/report
            self.alive[ip] = False
            self.ips.remove(ip)
        for ip in ips:
            if ip not in self.ips: self.ips.append(ip)
            self.stats.setdefault(ip, {"sent": 0, "errors": 0, "drops": 0, "reconnects": 0})
            self.wanted[ip] = True
            nm = "tx-%s" % ip.split(".")[-1]
            if not any(w.name == nm and w.is_alive() for w in self._workers):
                t = threading.Thread(target=self._worker, args=(ip, self.current.get(ip)),
                                     name=nm, daemon=True)
                self._workers.append(t); t.start()
                self.log("%s added to the mirror" % ip)

    # ---------- pause ----------
    # PAUSE FREEZES THE PRODUCER, NOT THE SENDERS. The wall keeps the last frame. The session
    # itself is held open by each Sender's own 4s NOOP keepalive thread (fm.Sender._ka_loop),
    # which runs whether or not frames flow; the 3s keyframe simply re-sends the frozen frame,
    # so a stale band still self-repairs while paused. Tearing the session down and
    # re-associating on resume is the one thing that wedges the network module - so we don't.
    def pause(self):
        self.pause_event.set()
        self.log("paused (wall holds the last frame)")

    def resume(self):
        self.pause_event.clear()
        self.log("resumed")

    @property
    def paused(self):
        return self.pause_event.is_set()

    def stop(self):
        self.pause_event.clear()
        self.stop_event.set()
        for w in self._workers: w.join(timeout=RECONNECT_WAIT + 5)
        if self._producer: self._producer.join(timeout=5)
        if getattr(self, "_reporter", None): self._reporter.join(timeout=self.report_sec + 2)
        for ip, s in list(self.current.items()):
            if s is not None:
                try: s.close()
                except Exception: pass
                self.current[ip] = None
        self.log("session stopped")

    # ---------- threads ----------
    def _make_stream(self):
        if getattr(self, "capture", "poll") != "stream":
            return None
        try:
            import pm_capture as cap
            backend, src = cap.make_source(getattr(self, "display_id", 1), fm.PANEL_W, fm.PANEL_H,
                                           getattr(self, "letterbox", True))
            if src is None:
                self.log("no stream backend; using poll capture"); return None
            self.log("capture: %s (push) on display %s -> %dx%d"
                     % (backend, getattr(self, "display_id", 1), fm.PANEL_W, fm.PANEL_H))
            return src
        except Exception as e:
            self.log("stream capture init failed (%s); using poll" % e); return None

    def _produce(self):
        last = None
        stream = self._make_stream()               # CGDisplayStream push source, or None -> poll
        last_seq = -1
        try:
          while not self.stop_event.is_set():
            if self.pause_event.is_set():
                last = None; last_seq = -1         # first frame after resume always goes out
                if self.stop_event.wait(0.1): break
                continue
            if stream is not None:
                seq, img = stream.get()            # event-driven: the OS pushed it, already panel-sized
                # Content rate from the source's OWN counter. seq advances once per frame the OS
                # pushes, at the rate the screen changes - it does not care how slowly we encode
                # or send. That makes it the one denominator the governor can divide by without
                # feeding back on itself (6292f48 divided by the send rate and ran away).
                _cn = time.time()
                if self._cf_t0 is None:
                    self._cf_t0 = _cn; self._cf_s0 = seq
                elif _cn - self._cf_t0 >= 0.5:
                    _cfps = (seq - self._cf_s0) / (_cn - self._cf_t0)
                    self._content_fps = _cfps if self._content_fps is None else (0.7 * self._content_fps + 0.3 * _cfps)
                    self._cf_t0 = _cn; self._cf_s0 = seq
                if img is None or (seq == last_seq and not self._mode_dirty):
                    time.sleep(0.004); continue
                fresh = (seq != last_seq)          # real content change, not our own re-encode
                last_seq = seq; self._mode_dirty = False
            else:
                try:
                    img = self.src()
                except Exception as e:
                    self.log("capture error: %s" % e); time.sleep(0.2); continue
                if img is None: time.sleep(0.02); continue
                h = zlib.crc32(img.tobytes())       # crc32 + keyframe safety net
                if h == last and not self._mode_dirty:
                    time.sleep(0.01); continue
                fresh = (h != last)
                last = h; self._mode_dirty = False
            if fresh:
                # Cadence of REAL content changes drives the governor budget. Re-encodes of the same
                # frame must not count, or the governor would think the screen is busy.
                _now = time.time()
                if self._prod_last is not None:
                    _dt = _now - self._prod_last
                    self._prod_ema = _dt if self._prod_ema is None else (0.7 * self._prod_ema + 0.3 * _dt)
                self._prod_last = _now
                self._gov_reencodes = 0
            try:
                scans = fm.build_scans(img)
            except Exception as e:
                self.log("encode error: %s" % e); time.sleep(0.2); continue
            # Adaptive-quality byte governor. Runs when the GUI enabled it (self.governor) OR the
            # CLI set the env path (fm.TARGET_KBPS). Trades quality to hold a per-frame budget so a
            # higher fps stays affordable. No-op unless JFIF is on.
            if fm.QPIN is not None:                       # quality pinned: governor off, report the truth
                self.gov_q = fm.QPIN
                self.gov_frame_bytes = sum(len(x[0]) for x in scans)
            elif fm.JFIF and (self.governor or fm.TARGET_KBPS > 0):
                target = self._gov_target_bytes()
                if target > 0:
                    fb = sum(len(x[0]) for x in scans)
                    before = fm._ADAPT_Q
                    fm.adapt_quality(fb, target)
                    self.gov_q = fm._ADAPT_Q; self.gov_frame_bytes = fb
                    # Converge NOW instead of waiting for the next screen change. On a slide the
                    # next change may be minutes away, which is what left quality stuck low.
                    if fm._ADAPT_Q != before and self._gov_reencodes < self.GOV_MAX_REENCODE:
                        self._gov_reencodes += 1
                        self._mode_dirty = True
                        continue          # re-encode this same frame at the new quality
            with self._slock:
                self._state["scans"] = scans; self._state["seq"] += 1
        except Exception:
            # A capture-backend raise (display disconnect, sleep/wake, permission loss mid-run)
            # must not silently kill the producer and leave the senders shipping stale keyframes
            # forever. Signal the session down; the GUI's refreshLog_ notices and tears down.
            import traceback
            self._producer_crashed = True
            self.log("producer crashed:\n" + traceback.format_exc())
            self.stop_event.set()
        finally:
            if stream is not None:
                try: stream.stop()                 # break the CFRunLoop / stopCapture; no leaked stream
                except Exception: pass

    def producer_alive(self):
        return self._producer is not None and self._producer.is_alive()

    def _worker(self, ip, s):
        had = s is not None
        prev = None; myseq = -1; fails = 0; last_send = 0.0
        ema = None; interval = self.min_interval; slow = 0; last_full = 0.0
        recon_fails = 0
        st = self.stats[ip]
        while not self.stop_event.is_set() and self.wanted.get(ip, True):
            if s is None:
                if not self.auto_reconnect: break
                self.alive[ip] = False
                # Settle: never re-associate within RECONNECT_WAIT of this ip's last close. Covers the
                # drop->reconnect path AND a set_wanted untick->retick - both would hammer the slot.
                lc = self._last_close.get(ip, 0.0)
                if lc > 0:
                    left = RECONNECT_WAIT - (time.time() - lc)
                    if left > 0 and self.stop_event.wait(left): break
                self.log("%s %s..." % (ip, "reconnecting" if had else "connecting"))
                try: s = fm._connect_with_retry(ip, tries=1)
                except Exception as e: s = None; self.log("%s reconnect error: %s" % (ip, e))
                if s:
                    self.current[ip] = s; self.alive[ip] = True
                    prev = None; myseq = -1; fails = 0; slow = 0   # fresh session, fresh counters
                    recon_fails = 0                                # connected: reset the backoff
                    if had: st["reconnects"] += 1
                    had = True
                    try:
                        if fm.pj_is_muted(ip): fm.pj_unblank(ip)
                    except Exception: pass
                    self.log("%s connected" % ip)
                else:
                    recon_fails += 1                              # escalating 30 -> 60 -> 120 (capped)
                    back = min(RECONNECT_BACKOFF0 * (2 ** (recon_fails - 1)), RECONNECT_BACKOFF_MAX)
                    self.log("%s connect failed; backing off %.0fs" % (ip, back))
                    if self.stop_event.wait(back): break
                    continue
            with self._slock:
                scans = self._state["scans"]; seq = self._state["seq"]
            if scans is None:
                time.sleep(0.004); continue
            now = time.time()
            force_full = (prev is None) or (now - last_full >= KEYFRAME_SEC)
            if seq == myseq and not force_full:      # keyframe still fires on a static screen
                time.sleep(0.004); continue
            myseq = seq
            idxs = (list(range(len(scans))) if force_full
                    else [i for i in range(len(scans)) if scans[i][0] != prev[i][0]])
            if not idxs:
                prev = scans; continue
            dt = time.time() - last_send
            if dt < interval: time.sleep(interval - dt)
            try:
                t_send = time.time()
                s.send_strips(scans, idxs)
                dur = time.time() - t_send
                fails = 0; slow = 0; last_send = t_send   # pace from send-START: period = max(interval, send)
                # sendall blocks until the projector accepts -> its duration IS the drain rate.
                prev = scans                      # only on success (v1.1 stale-band bug)
                if force_full: last_full = now
                ema = dur if ema is None else (0.7 * ema + 0.3 * dur)
                interval = max(self.min_interval, ema)
                st["sent"] += 1
                st["bytes"] = st.get("bytes", 0) + sum(len(scans[i][0]) for i in idxs)
                st["strips"] = st.get("strips", 0) + len(idxs)
                st["send_ms"] = round(dur * 1000, 1); st["interval"] = round(interval, 3)
            except Exception as e:
                if "timed out" in str(e).lower():
                    slow += 1; myseq = -1        # retry this frame; prev was not advanced
                    # seed from a nonzero base: in free-run interval is 0 until the FIRST
                    # successful send, and 0*2 is no backoff (field: 'backing off to 0.00s')
                    interval = min(max(interval, 0.25) * 2.0, 3.0); st["slow"] = st.get("slow", 0) + 1
                    self.log("%s slow send (%d) - backing off to %.2fs" % (ip, slow, interval))
                    if slow < 4:
                        continue
                fails += 1; myseq = -1; st["errors"] += 1
                self.log("%s send err: %s (%d/%d)" % (ip, e, fails, FAIL_LIMIT))
                if fails >= FAIL_LIMIT:
                    st["drops"] += 1; self.alive[ip] = False
                    self.log("%s DROPPED" % ip)
                    try: s.close()
                    except Exception: pass
                    self._last_close[ip] = time.time()       # start the settle clock
                    s = None; self.current[ip] = None
                    continue                                     # settle-before-connect enforces the 30s
        if s is not None:
            try: s.close()
            except Exception: pass
            self._last_close[ip] = time.time()
            self.current[ip] = None

    # ---------- reporting ----------
    def _report_loop(self):
        # Restores the v1.x 'rates:' log line (dropped when the GUI producer was unified out).
        # produced/s = frames encoded and published; per-projector sent/s + the last send_ms
        # (whose reciprocal is that unit's bandwidth-bound fps ceiling).
        with self._slock: pseq = self._state["seq"]
        psent = {ip: self.stats.get(ip, {}).get("sent", 0) for ip in self.ips}
        pbytes = {ip: self.stats.get(ip, {}).get("bytes", 0) for ip in self.ips}
        pstrip = {ip: self.stats.get(ip, {}).get("strips", 0) for ip in self.ips}
        pt = time.time()
        while not self.stop_event.wait(self.report_sec):
            now = time.time(); dt = now - pt
            if dt <= 0: continue
            with self._slock: nseq = self._state["seq"]
            prod = (nseq - pseq) / dt
            parts = []
            for ip in self.ips:
                st = self.stats.get(ip, {})
                nsent = st.get("sent", 0)
                sps = (nsent - psent.get(ip, 0)) / dt
                nb = st.get("bytes", 0); kbps = (nb - pbytes.get(ip, 0)) / 1024.0 / dt
                nst = st.get("strips", 0)
                dsent = nsent - psent.get(ip, 0)
                spf = ((nst - pstrip.get(ip, 0)) / dsent) if dsent > 0 else 0.0
                psent[ip] = nsent; pbytes[ip] = nb; pstrip[ip] = nst
                tail = ip.split(".")[-1]
                if not self.alive.get(ip, False):
                    parts.append(".%s reconnecting" % tail)
                else:
                    sm = st.get("send_ms", 0.0)
                    cap = (1000.0 / sm) if sm else 0.0
                    parts.append(".%s %.1f/s %.0fkB/s %.1fstrip send~%.0fms(cap~%.0f/s)"
                                 % (tail, sps, kbps, spf, sm, cap))
            pseq = nseq; pt = now
            q = self.gov_q if self.gov_q is not None else "-"
            fk = (self.gov_frame_bytes / 1024.0) if self.gov_frame_bytes else 0.0
            self.log("rates: produced=%.1f/s q=%s frame=%.0fkB  %s" % (prod, q, fk, "  ".join(parts)))

    def fps(self, ip):
        el = (time.time() - self.started_at) if self.started_at else 0
        return (self.stats[ip]["sent"] / el) if el > 0 else 0.0

    def summary(self):
        out = {}
        for ip in self.ips:
            st = dict(self.stats.get(ip, {}))
            st["fps"] = round(self.fps(ip), 2)
            st["alive"] = self.alive.get(ip, False)
            out[ip] = st
        return out
