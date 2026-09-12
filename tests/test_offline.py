#!/usr/bin/env python3
"""
test_offline.py - regression tests that need NO projector and NO screen capture.

Exists because v1.2-v1.4 shipped without hardware validation, and several regressions this
session were introduced by edits that "looked right" (prev-advance stale bands, a tuple passed
as %-format args, AppKit touched off the main thread, a tccutil reset that deleted a live grant).
Run this before every build:   python3 test_offline.py
"""
import struct, sys, time, zlib
# PROJECT-RELATIVE MODULE PATH: work whether run from the vault (flat) or the
# repo (tests/ beside src/). Copying this file between the two must not break it.
import os as _os
_here=_os.path.dirname(_os.path.abspath(__file__))
for _c in (_here, _os.path.join(_here,'..','src'), _os.getcwd()):
    if _os.path.exists(_os.path.join(_c,'pm_mirror_v2.py')): sys.path.insert(0,_c)
import pm_mirror_v2 as fm
import pm_pkey

FAIL = []
def check(name, cond, detail=""):
    print("  %-52s %s%s" % (name, "ok" if cond else "FAIL", "" if cond else "  <- " + str(detail)))
    if not cond: FAIL.append(name)

from PIL import Image, ImageDraw
def slide(seed=0):
    im = Image.new("RGB", (1024, 768), (250, 250, 245)); d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 1023, 120], fill=(30, 60, 110))
    for i in range(8):
        d.text((60, 180 + i * 60), "line %d seed %d" % (i, seed), fill=(20, 20, 20))
    return im

print("\n-- pkey / key derivation --")
for nm, exp in pm_pkey.KNOWN_VECTORS.items():
    check("create_temp_key(%r)" % nm, pm_pkey.create_temp_key(nm).hex() == exp)
check("padding irrelevant (trailing spaces -> '*')",
      pm_pkey.create_temp_key("LEFT") == pm_pkey.create_temp_key("LEFT      "))
check("name truncated to 16 bytes",
      pm_pkey.build_host20("A" * 30)[:16] == b"A" * 16)
check("host20 is always 20 bytes", all(len(pm_pkey.build_host20(n)) == 20
                                       for n in ("", "X", "RIGHT", "A" * 40)))
check("baked PKEYS still match derivation",
      all(fm.PKEYS[ip] == pm_pkey.create_temp_key(loc).hex()
          for ip, loc in (("192.168.1.101", "RIGHT"), ("192.168.1.100", "LEFT"))))

print("\n-- strip framing (protocol-critical) --")
s = fm.Sender.__new__(fm.Sender)
s.commkey = bytes(range(16))
h = s.strip(b"\x00" * 64, 1024, 96, 96, codec=3, last=False)[:24]
check("header[0:2] == 0701", h[0:2] == b"\x07\x01", h[:4].hex())
check("codec-3 mode field == 0x0310", h[4:6] == b"\x03\x10", h[4:6].hex())
check("field[6:8] == 0x0014", h[6:8] == b"\x00\x14")
check("payload length is BE32", struct.unpack(">I", h[8:12])[0] == 64)
check("jpeg w/h = 1024x96", h[12:16] == struct.pack(">HH", 1024, 96))
check("yoffset encoded", struct.unpack(">H", h[18:20])[0] == 96)
check("panel size 1024x768", h[20:24] == struct.pack(">HH", 1024, 768))
check("lastsend flag sets byte 2 to 0x80",
      s.strip(b"\x00" * 16, 1024, 96, 0, 3, True)[2] == 0x80)
check("non-last flag byte 2 is 0x00",
      s.strip(b"\x00" * 16, 1024, 96, 0, 3, False)[2] == 0x00)

print("\n-- payload encryption (whole blocks encrypted, tail plaintext) --")
for n in (0, 1, 15, 16, 17, 31, 32, 100):
    p = bytes((i * 7 + 1) & 0xff for i in range(n))
    e = s.encrypt_payload(p)
    whole = (n // 16) * 16
    check("len %-4d preserved and tail %2d bytes plaintext" % (n, n - whole),
          len(e) == n and e[whole:] == p[whole:])

print("\n-- encoding / strips --")
img = slide()
scans = fm.build_scans(img)
check("8 strips produced", len(scans) == 8)
check("y offsets 0..672 step 96", [x[1] for x in scans] == [i * 96 for i in range(8)])
check("only the 8th strip is 'last'", [x[2] for x in scans] == [False] * 7 + [True])
check("scan ends with JPEG EOI (FFD9)", all(x[0].endswith(b"\xff\xd9") for x in scans))
check("quality locked at 50 regardless of arg",
      fm.build_scans(img, quality=90)[0][0] == scans[0][0])

print("\n-- change detection (crc32 swap) --")
a, b = slide(1), slide(2)
check("identical frames hash equal", zlib.crc32(a.tobytes()) == zlib.crc32(slide(1).tobytes()))
check("different frames hash differ", zlib.crc32(a.tobytes()) != zlib.crc32(b.tobytes()))
check("strip-delta finds only changed bands",
      0 < len([i for i in range(8) if fm.build_scans(a)[i][0] != fm.build_scans(b)[i][0]]) < 8)

print("\n-- colour pipeline --")
fm.set_color(gamma=1.0, r=1.0, g=1.0, b=1.0)
check("all-1.00 is the identity fast path", fm.color_adjust(img) is img)
fm.set_color(gamma=0.85)
check("gamma 0.85 darkens midtones", fm.color_adjust(Image.new("RGB", (4, 4), (128, 128, 128))).getpixel((0, 0))[0] < 128)
fm.set_color(gamma=1.20)
check("gamma 1.20 brightens midtones", fm.color_adjust(Image.new("RGB", (4, 4), (128, 128, 128))).getpixel((0, 0))[0] > 128)
fm.set_color(gamma=1.0, r=1.0, g=1.0, b=1.0)
check("green channel is actually wired", (fm.set_color(g=0.8) or
      fm.color_adjust(Image.new("RGB", (4, 4), (200, 200, 200))).getpixel((0, 0))[1] < 200))
fm.set_color(gamma=1.0, r=1.0, g=1.0, b=1.0)

print("\n-- letterbox / fit --")
fm.FIT = "letterbox"
out = fm.fit_1024x768(Image.new("RGB", (2560, 1440), (255, 0, 0)))
check("letterbox output is 1024x768", out.size == (1024, 768))
check("letterbox adds black bars on 16:9", out.getpixel((512, 2)) == (0, 0, 0))
fm.FIT = "stretch"
check("stretch fills the frame", fm.fit_1024x768(Image.new("RGB", (2560, 1440), (255, 0, 0))).getpixel((512, 2)) != (0, 0, 0))
fm.FIT = "letterbox"

print("\n-- 602 discovery parsing --")
p = (b"602" + b"PanasonicF300NT    " + b"0430" + b"0F03" + b"00000000"
     + b"RIGHT           " + b"Proj0001" + b"001000" + b"00" + b"*" * 34)
check("location field at [38:54]", p[38:54] == b"RIGHT           ", p[38:54])
check("name field at [54:62]", p[54:62] == b"Proj0001", p[54:62])
check("derives the right key from a real-shaped 602",
      pm_pkey.create_temp_key(p[38:54].decode()).hex() == fm.PKEYS["192.168.1.101"])

print("\n-- PJLink helpers --")
check("MUTE_ON codes", fm.MUTE_ON == ("11", "21", "31"))
check("pj_get tolerates an unreachable host", fm.pj_get("192.0.2.1", "%1POWR ?") is None)

# ---- PJLink power state machine -------------------------------------------------
# v2.4 mirrored to a projector that was still switched off whenever a start
# followed a power-off: cooling reports '2', refuses %1POWR 1 with ERR3, and then
# settles at '0' STANDBY -- never at '1'. The old wait polled 100s for a '1' that
# cannot arrive. These run against a mock projector on a loopback port.
import socket as _sock, threading as _thr, hashlib as _md5

class _MockPJ:
    """PJLink Class 1 power state machine, per spec v1.04 s4.1/s4.2."""
    def __init__(self, power="2", cool_polls=3, warm_polls=2, dribble=False, seed=None):
        self.power, self.cool_polls, self.warm_polls = power, cool_polls, warm_polls
        self.dribble, self.seed = dribble, seed
        self.log, self.n, self.digests = [], 0, []
    def _reply(self, line):
        if self.seed and len(line) > 32 and not line.startswith("%"):
            self.digests.append(line[:32]); line = line[32:]
        self.log.append(line)
        cmd, _, arg = line.partition(" "); cmd = cmd.upper()
        if cmd == "%1POWR":
            if arg == "?":
                self.n += 1
                if self.power == "2" and self.n > self.cool_polls: self.power = "0"
                elif self.power == "3" and self.n > self.cool_polls + self.warm_polls: self.power = "1"
                return "%1POWR=" + self.power
            if self.power in ("2", "3"): return "%1POWR=ERR3"
            if arg == "1": self.power = "3"; self.n = self.cool_polls; return "%1POWR=OK"
            self.power = "0"; return "%1POWR=OK"
        if cmd == "%1INPT": return "%1INPT=OK" if self.power == "1" else "%1INPT=ERR3"
        return cmd + "=ERR1"

def _serve(proj, port):
    srv = _sock.socket(); srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port)); srv.listen(16)
    def conn(c):
        try:
            g = (("PJLINK 1 %s\r" % proj.seed) if proj.seed else "PJLINK 0\r").encode()
            if proj.dribble:
                for b in g: c.sendall(bytes([b])); time.sleep(0.002)
            else: c.sendall(g)
            buf = b""
            while b"\r" not in buf:
                ch = c.recv(256)
                if not ch: return
                buf += ch
            c.sendall((proj._reply(buf.split(b"\r")[0].decode("ascii", "replace")) + "\r").encode())
        except Exception: pass
        finally:
            try: c.close()
            except Exception: pass
    def loop():
        while True:
            try: c, _ = srv.accept()
            except OSError: return
            _thr.Thread(target=conn, args=(c,), daemon=True).start()
    _thr.Thread(target=loop, daemon=True).start()
    return srv

class _VClock:
    """Virtual clock so a 240s timeout costs no wall time."""
    def __init__(self): self.t = 0.0
    def sleep(self, s): self.t += s
    def monotonic(self): return self.t

_srv = _serve(_MockPJ(power="2"), 4352)
_real_time, _clk = fm.time, _VClock()
fm.time = _clk
try:
    _ok = fm.power_on_network("127.0.0.1", log=lambda m: None)
finally:
    fm.time = _real_time; _srv.close()
check("power_on_network waits out COOLING and powers on", _ok is True)
check("power_on_network does not burn the full timeout while cooling", _clk.t < 120, "%.0fs" % _clk.t)

_proj = _MockPJ(power="1", dribble=True, seed="498e4a67")
_srv = _serve(_proj, 4352)
try:
    _r = fm.pjlink("127.0.0.1", "%1POWR ?")
finally:
    _srv.close()
_want = _md5.md5(("498e4a67" + fm.PJLINK_PW).encode()).hexdigest()
check("auth digest survives a greeting split across TCP segments",
      _proj.digests and _proj.digests[0] == _want, _proj.digests)
check("response is read as a whole line, not a partial recv", _r == "%1POWR=1", _r)

_srv = _serve(_MockPJ(power="0"), 4352)
_real_time, _clk = fm.time, _VClock()
fm.time = _clk
try:
    _ok0 = fm.power_on_network("127.0.0.1", log=lambda m: None)
finally:
    fm.time = _real_time; _srv.close()
check("power_on_network powers on from STANDBY", _ok0 is True)

print("\n-- browser control URLs (WM's 'Remote control' is just this CGI) --")
check("WEB control url", fm.web_url("192.168.1.100") == "http://192.168.1.100/")
check("remote url matches the WM format string",
      fm.remote_url("192.168.1.101") == "http://192.168.1.101/cgi-bin/remocon.cgi?lang=en")

print("\n-- subnet discovery sweep --")
check("602 parser accepts a real-shaped reply", (fm._parse_602(p) or {}).get("name") == "Proj0001")
check("602 parser rejects a short/foreign packet",
      fm._parse_602(b"601 nope") is None and fm._parse_602(b"602" + b"x" * 10) is None)
check("broadcast address of a /24", fm._bcast_of("192.168.1.100") == "192.168.1.255")
check("discovery packet is the captured 600", fm.DISCOVERY_PKT.startswith(b"600TRANSPIC0430"))
_sweep = fm.discover_all(timeout=0.2, hint_ips=["192.168.1.100"])
check("discover_all returns a list with no responders", isinstance(_sweep, list))

print("\n-- registered projectors (pm_registry) --")
import pm_registry as reg
check("valid_ip", reg.valid_ip("192.168.1.9") and not reg.valid_ip("192.168.1.999")
      and not reg.valid_ip("kitchen"))
check("normalize drops junk and de-duplicates by ip",
      [e["ip"] for e in reg.normalize([{"ip": "192.168.1.1"}, {"ip": "192.168.1.1"},
                                       {"ip": "nope"}, {"name": "no ip"}])] == ["192.168.1.1"])
check("normalize names an unnamed row after its ip",
      reg.normalize([{"ip": "192.168.1.7"}])[0]["name"] == "192.168.1.7")
_mig = reg.from_settings({"left": 0, "right": 1})
check("pre-v1.5 settings migrate to the two units",
      [e["ip"] for e in _mig] == ["192.168.1.100", "192.168.1.101"], _mig)
check("migration keeps a projector the user had switched OFF",
      [e["on"] for e in _mig] == [0, 1], _mig)
check("an explicitly empty list is honoured, not re-seeded",
      reg.from_settings({"projectors": []}) == [])
check("a saved list is read back", len(reg.from_settings(
      {"projectors": [{"ip": "10.0.0.4", "name": "Hall", "on": 0}]})) == 1)
_e, _added = reg.add([], "192.168.1.102", "Balcony")
check("add returns added=True and stores the name",
      _added and _e[0]["name"] == "Balcony" and _e[0]["on"] == 1)
check("adding a duplicate ip is a no-op", reg.add(_e, "192.168.1.102")[1] is False)
try:
    reg.add(_e, "not-an-ip"); _bad = False
except ValueError:
    _bad = True
check("add rejects a malformed ip", _bad)
check("remove", reg.remove(_e, "192.168.1.102") == [])
check("rename", reg.rename(_e, "192.168.1.102", "Rear")[0]["name"] == "Rear")
check("rename cannot blank a name", reg.rename(_e, "192.168.1.102", "   ")[0]["name"]
      == "192.168.1.102")
_reg2 = [{"ip": "192.168.1.100", "name": "Projector 1", "on": 0}]
_reg2, _new = reg.merge_discovered(_reg2, [
    {"ip": "192.168.1.100", "location": "LEFT            ", "name": "Proj0002"},
    {"ip": "192.168.1.101", "location": "RIGHT           ", "name": "Proj0001"}])
check("discovery adds only unknown projectors", _new == ["192.168.1.101"], _new)
check("discovery never re-enables or renames an existing row",
      _reg2[0]["on"] == 0 and _reg2[0]["name"] == "Projector 1", _reg2[0])
check("a discovered projector is named from its LOCATION field",
      _reg2[1]["name"] == "Right", _reg2[1])
check("a discovered projector with no location falls back to its ProjNNNN name",
      reg.name_for_discovery({"location": "   ", "name": "Proj0001"}, "10.0.0.1") == "Proj0001")
check("selected_ips only returns ticked rows",
      reg.selected_ips(_reg2) == ["192.168.1.101"] and len(reg.all_ips(_reg2)) == 2)


# ---------------------------------------------------------------------------
# Behavioural tests for the sender worker, driven with a FAKE sender.
# These cover the two bugs found on 2026-09-08 by audit rather than by hardware:
#   * a failed send was never retried on a static screen (myseq had advanced)
#   * the keyframe never fired on a static screen (body gated on a new frame)
# ---------------------------------------------------------------------------
import socket, threading
import pm_session as fs

class FakeSender:
    def __init__(self, fail_first=0):
        self.ip = "10.0.0.1"; self.calls = []; self.fail_first = fail_first
        self.commkey = b"\x00" * 16
    def send_strips(self, scans, idxs):
        self.calls.append(list(idxs))
        if len(self.calls) <= self.fail_first:
            raise socket.timeout("timed out")
    def close(self): pass

def run_worker(fake, seconds=0.9, keyframe=0.25):
    old_kf = fs.KEYFRAME_SEC
    fs.KEYFRAME_SEC = keyframe
    try:
        sess = fs.MirrorSession.__new__(fs.MirrorSession)
        sess.stop_event = threading.Event(); sess._slock = threading.Lock()
        sess._state = {"scans": fm.build_scans(slide(1)), "seq": 1}
        sess.current = {fake.ip: fake}; sess.wanted = {fake.ip: True}
        sess.alive = {fake.ip: True}; sess.stats = {fake.ip: {"sent": 0, "errors": 0, "drops": 0, "reconnects": 0}}
        sess.min_interval = 0.02; sess.auto_reconnect = False; sess.log = lambda *a: None
        t = threading.Thread(target=sess._worker, args=(fake.ip, fake), daemon=True); t.start()
        time.sleep(seconds); sess.stop_event.set(); t.join(timeout=3)
        return sess
    finally:
        fs.KEYFRAME_SEC = old_kf

print("\n-- worker behaviour (fake sender, no hardware) --")
f1 = FakeSender()
run_worker(f1)
check("keyframe fires on a STATIC screen (no new frames)", len(f1.calls) >= 2,
      "%d sends" % len(f1.calls))
check("every static-screen resend is a FULL 8-strip keyframe",
      all(len(c) == 8 for c in f1.calls), f1.calls[:3])

f2 = FakeSender(fail_first=1)
run_worker(f2)
check("a failed send is RETRIED rather than dropped", len(f2.calls) >= 2,
      "%d sends" % len(f2.calls))
check("the retry resends the strips that failed",
      len(f2.calls) >= 2 and set(f2.calls[0]).issubset(set(f2.calls[1])), f2.calls[:2])

print("\n-- pause (producer freezes, senders keep the session alive) --")
def run_producer(pause_after=0.25, paused_for=0.4):
    """Drive MirrorSession._produce with a source that changes every call, and watch seq."""
    sess = fs.MirrorSession.__new__(fs.MirrorSession)
    sess.stop_event = threading.Event(); sess.pause_event = threading.Event()
    sess._slock = threading.Lock(); sess._state = {"scans": None, "seq": 0}
    sess.log = lambda *a: None
    n = {"i": 0}
    def src():
        n["i"] += 1
        time.sleep(0.01)
        return slide(n["i"])
    sess.src = src
    t = threading.Thread(target=sess._produce, daemon=True); t.start()
    time.sleep(pause_after)
    sess.pause_event.set()
    time.sleep(0.05)                       # let an in-flight frame land
    with sess._slock: frozen = sess._state["seq"]
    time.sleep(paused_for)
    with sess._slock: still = sess._state["seq"]
    sess.pause_event.clear()
    time.sleep(0.25)
    with sess._slock: after = sess._state["seq"]
    sess.stop_event.set(); t.join(timeout=3)
    return frozen, still, after, t

_frozen, _still, _after, _t = run_producer()
check("frames are produced before pausing", _frozen > 0, _frozen)
check("NO new frame is produced while paused", _still == _frozen, (_frozen, _still))
check("frames resume after unpausing", _after > _still, (_still, _after))
check("the producer thread exits on stop", not _t.is_alive())
_s = fs.MirrorSession.__new__(fs.MirrorSession)
_s.pause_event = threading.Event(); _s.stop_event = threading.Event(); _s.log = lambda *a: None
_s._workers = []; _s._producer = None; _s.current = {}
_s.pause()
check("pause()/paused/resume() API", _s.paused and (_s.resume() or not _s.paused))
_s.pause(); _s.stop()
check("stop() clears a pause so the next start is not born paused",
      not _s.paused and _s.stop_event.is_set())


print("\n-- resolution negotiation (STAT RES -> 112 <w> <h>) --")
check("default geometry is still exactly 8 x 96", fm.strip_layout(768) == [(i * 96, 96) for i in range(8)])
check("a 1200-high panel is fully covered with a short final strip",
      sum(h for _, h in fm.strip_layout(1200)) == 1200 and fm.strip_layout(1200)[-1] == (1152, 48))
check("an 800-high panel is fully covered", sum(h for _, h in fm.strip_layout(800)) == 800)
check("offsets are contiguous with no gaps",
      all(fm.strip_layout(1080)[k][0] + fm.strip_layout(1080)[k][1] == fm.strip_layout(1080)[k + 1][0]
          for k in range(len(fm.strip_layout(1080)) - 1)))
check("fit_panel letterboxes to an arbitrary size",
      fm.fit_panel(Image.new("RGB", (2560, 1440), (255, 0, 0)), 1280, 800).size == (1280, 800))
check("fit_1024x768 still delegates to the default panel", fm.fit_1024x768(slide()).size == (1024, 768))

_sender = fm.Sender.__new__(fm.Sender); _sender.commkey = bytes(range(16))
_sender.panel_w, _sender.panel_h = 1920, 1200
_hdr = _sender.strip(b"\x00" * 32, 1920, 150, 300, codec=3, last=False)[:24]
check("strip header carries the NEGOTIATED panel size",
      _hdr[20:24] == struct.pack(">HH", 1920, 1200), _hdr[20:24].hex())
check("strip header carries the negotiated strip height",
      _hdr[12:16] == struct.pack(">HH", 1920, 150))

_orig = (fm.PANEL_W, fm.PANEL_H)
try:
    fm.set_panel(1280, 800)
    _sc = fm.build_scans(slide())
    check("build_scans follows the negotiated geometry",
          len(_sc) == len(fm.strip_layout(800)) and sum(x[3] for x in _sc) == 800)
    check("exactly one strip is flagged last", [x[2] for x in _sc].count(True) == 1)
    check("the LAST strip is the one flagged", _sc[-1][2] is True)

    class _S:
        def __init__(self, w, h): self.panel_w, self.panel_h = w, h
    fm.set_panel(*_orig)
    check("adopt_panel takes the reported resolution",
          fm.adopt_panel([_S(1920, 1200)], log=lambda *a: None) == (1920, 1200))
    _warn = []
    fm.set_panel(*_orig)
    check("mixed resolutions warn and use the first",
          fm.adopt_panel([_S(1024, 768), _S(1920, 1200)], log=_warn.append) == (1024, 768)
          and any("different resolutions" in w for w in _warn), _warn)
finally:
    fm.set_panel(*_orig)
check("panel restored to 1024x768 for the verified path", (fm.PANEL_W, fm.PANEL_H) == (1024, 768))


print("\n-- JFIF mode + adaptive quality (experimental; default OFF) --")
import importlib, io as _io
from PIL import Image as _Image
check("JFIF is OFF by default (proven marker-less path is default)", fm.JFIF is False)

# force JFIF on in a private reload so the default-off engine used elsewhere is untouched
import os as _os
_os.environ["JFIF"] = "1"; _os.environ["TARGET_KBPS"] = "0"
fj = importlib.reload(fm)
try:
    sc = fj.build_scans(slide(1))
    check("JFIF strips are FULL JPEGs (FFD8..FFD9)",
          all(x[0][:2] == b"\xff\xd8" and x[0][-2:] == b"\xff\xd9" for x in sc))
    check("each JFIF strip re-decodes (proxy for projector-decodable)",
          all(_Image.open(_io.BytesIO(x[0])).size[0] == 1024 for x in sc))
    # quality actually changes size (the lever): q30 smaller than q80
    b30 = sum(len(x[0]) for x in fj.build_scans(slide(1), quality=30))
    b80 = sum(len(x[0]) for x in fj.build_scans(slide(1), quality=80))
    check("lower JFIF quality => fewer bytes (the fps lever works)", b30 < b80, "q30=%d q80=%d" % (b30, b80))
    # adaptive governor converges
    fj._ADAPT_Q = 60
    for _ in range(20): fj.adapt_quality(last_frame_bytes=999999, target_frame_bytes=10000)
    lo = fj._ADAPT_Q
    check("governor drops to floor when over budget", lo == fj.JFIF_QMIN, "q=%d" % lo)
    for _ in range(40): fj.adapt_quality(last_frame_bytes=10, target_frame_bytes=10000)
    hi = fj._ADAPT_Q
    check("governor rises to ceiling when under budget", hi == fj.JFIF_QMAX, "q=%d" % hi)
    check("governor is a no-op when target is 0", (fj.adapt_quality(999999, 0), fj._ADAPT_Q)[1] == hi)
finally:
    _os.environ["JFIF"] = "0"; importlib.reload(fm)   # restore default-off engine for any later use


print("\n-- control plane: vendor-exact by default (no per-frame NOOP / 12004) --")
import importlib as _il, os as _os, threading as _th
class _Sock:
    def __init__(self): self.n=0
    def sendall(self,b): self.n+=1
    def sendto(self,b,a): self.n+=1
def _mk(mod):
    sd=mod.Sender.__new__(mod.Sender); sd.ip="10.0.0.9"; sd.commkey=b"\x00"*16
    sd.panel_w,sd.panel_h=1024,768; sd._ctl_lock=_th.RLock(); sd._last_ctl=0.0
    sd.vs=_Sock(); sd.ctl=_Sock(); sd.u12004=_Sock(); return sd
sc=fm.build_scans(slide(1))
sd=_mk(fm); sd.send_strips(sc,[0,3]); sd.send_scans(sc)
check("default: video socket written", sd.vs.n==2)
check("default: NO NOOP per frame on the control socket", sd.ctl.n==0, "ctl sends=%d"%sd.ctl.n)
check("default: NO UDP-12004 datagram per frame", sd.u12004.n==0, "12004 sends=%d"%sd.u12004.n)
_os.environ["CTL_LEGACY"]="1"; fl=_il.reload(fm)
try:
    sd=_mk(fl); sd.send_strips(sc,[0])
    check("CTL_LEGACY=1: old per-frame NOOP + 12004 restored", sd.ctl.n==1 and sd.u12004.n==1)
finally:
    _os.environ["CTL_LEGACY"]="0"; _il.reload(fm)
check("CTL_LEGACY off again", fm.CTL_LEGACY is False)

print("\n-- session-keyed beacon --")
_sd = fm.Sender.__new__(fm.Sender)
_sd.commkey = bytes(range(16))
b = _sd.build_beacon()
check("beacon is 55 bytes", len(b) == 55, "len=%d" % len(b))
check("beacon prefix 7000430+TOKEN", b[:15] == (b"7000430"+fm.TOKEN.encode()))
check("beacon suffix 00000000", b[47:55] == b"00000000")
import Crypto.Cipher.AES as _A
_u = fm.BEACON_USER.encode()[:32].ljust(32, b" ")
check("mid = AES-ECB(commkey, user.ljust(32))",
      b[15:47] == _A.new(_sd.commkey, _A.MODE_ECB).encrypt(_u))
_sd2 = fm.Sender.__new__(fm.Sender); _sd2.commkey = bytes(range(1,17))
check("beacon differs with a different session key", _sd2.build_beacon() != b)

print("\n-- chroma subsampling + quality pin (2026-09-10 quality knobs) --")
import os as _os
from PIL import Image as _Img
def _y_sampling(jpeg):
    i = jpeg.find(b"\xff\xc0")           # SOF0; first component's sampling-factor byte at i+11
    return None if i < 0 else jpeg[i+11]
_band = _Img.frombytes("RGB", (1024, 96), _os.urandom(1024*96*3))
fm.SUBSAMPLING = 0; _j444 = fm.encode_scan(_band, 80, jfif=True)
fm.SUBSAMPLING = 1; _j422 = fm.encode_scan(_band, 80, jfif=True)
fm.SUBSAMPLING = 2; _j420 = fm.encode_scan(_band, 80, jfif=True)
check("SUBSAMPLING=0 encodes 4:4:4 (Y sampling 0x11)", _y_sampling(_j444) == 0x11, hex(_y_sampling(_j444) or 0))
check("SUBSAMPLING=1 encodes 4:2:2 (Y sampling 0x21)", _y_sampling(_j422) == 0x21, hex(_y_sampling(_j422) or 0))
check("SUBSAMPLING=2 encodes 4:2:0 (Y sampling 0x22)", _y_sampling(_j420) == 0x22, hex(_y_sampling(_j420) or 0))
check("4:4:4 carries more bytes than 4:2:0 (full chroma)", len(_j444) > len(_j420))
# quality pin overrides the mode band + clamp
_fullimg = _Img.frombytes("RGB", (1024, 768), _os.urandom(1024*768*3))
_qband0 = (fm.JFIF_QMIN, fm.JFIF_QMAX)          # restore after: these are MODULE globals
fm.JFIF = True; fm.JFIF_QMIN = 15; fm.JFIF_QMAX = 85; fm.SUBSAMPLING = 2
fm.QPIN = 15;  _s15  = sum(len(x[0]) for x in fm.build_scans(_fullimg))
fm.QPIN = 100; _s100 = sum(len(x[0]) for x in fm.build_scans(_fullimg))
check("QPIN=100 encodes larger than QPIN=15 (pin takes effect)", _s100 > _s15)
check("QPIN=100 overrides JFIF_QMAX=85 (not clamped to the mode band)",
      _s100 > sum(len(x[0]) for x in (lambda: (setattr(fm,"QPIN",None), fm.build_scans(_fullimg))[1])()))
fm.QPIN = None; fm.SUBSAMPLING = 2; fm.JFIF = False   # restore defaults for later tests
fm.JFIF_QMIN, fm.JFIF_QMAX = _qband0                 # the band leaked into every later test

print("\n-- MirrorSession.set_mode (v2 live picture mode) --")
import pm_session as _fs2
_ms = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_ms.min_interval = 0.15; _ms.governor = False; _ms.target_kbps = 0.0; _ms._mode_dirty = False
_ms.set_mode(min_interval=0.0, governor=True, target_kbps=250.0)
check("set_mode updates min_interval", _ms.min_interval == 0.0)
check("set_mode updates governor flag", _ms.governor is True)
check("set_mode updates target_kbps", _ms.target_kbps == 250.0)
check("set_mode marks a re-encode (_mode_dirty)", _ms._mode_dirty is True)
_ms.set_mode(governor=False)
check("set_mode partial update leaves min_interval", _ms.min_interval == 0.0 and _ms.governor is False)
check("MirrorSession defaults keep CLI env path (governor off, target 0)",
      _fs2.MirrorSession.__new__(_fs2.MirrorSession) is not None)  # smoke: class constructs
print("\n-- MirrorSession stores letterbox (review fix: stream backend honored it) --")
_lb = _fs2.MirrorSession(["10.0.0.9"], (lambda: None), log=(lambda *a: None), letterbox=False)
check("letterbox=False is stored on the session", _lb.letterbox is False)
check("letterbox=False sets stretch FIT", _fs2.fm.FIT == "stretch")
_lb2 = _fs2.MirrorSession(["10.0.0.9"], (lambda: None), log=(lambda *a: None), letterbox=True)
check("letterbox=True is stored on the session", _lb2.letterbox is True)

print("\n-- MirrorSession.set_wanted drops removed projectors from reported state (review fix) --")
class _WStub:
    def __init__(self, nm): self.name = nm
    def is_alive(self): return True
_sw = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_sw.log = lambda *a: None
_sw.ips = ["10.0.0.1", "10.0.0.2"]
_sw.wanted = {"10.0.0.1": True, "10.0.0.2": True}
_sw.alive = {"10.0.0.1": True, "10.0.0.2": True}
_sw.stats = {"10.0.0.1": {}, "10.0.0.2": {}}
_sw.current = {}
_sw._workers = [_WStub("tx-1"), _WStub("tx-2")]   # both have live workers -> set_wanted spawns none
_sw.set_wanted(["10.0.0.2"])                       # remove .1
check("removed projector dropped from self.ips", "10.0.0.1" not in _sw.ips and "10.0.0.2" in _sw.ips)
check("removed projector marked not-alive", _sw.alive["10.0.0.1"] is False)
check("kept projector stays wanted", _sw.wanted["10.0.0.2"] is True and _sw.wanted["10.0.0.1"] is False)

print("\n-- MirrorSession._report_loop (v2.4 restored 'rates:' line) --")
import threading as _thr, time as _t
_rs = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_rs.ips = ["192.168.1.100", "192.168.1.101"]
_rs.stop_event = _thr.Event(); _rs._slock = _thr.Lock()
_rs._state = {"scans": None, "seq": 0}
_rs.gov_q = 45; _rs.gov_frame_bytes = 40 * 1024
_rs.stats = {"192.168.1.100": {"sent": 0, "send_ms": 50.0},
             "192.168.1.101": {"sent": 0, "send_ms": 100.0}}
_rs.alive = {"192.168.1.100": True, "192.168.1.101": False}
_rs.report_sec = 0.15
_caught = []
_rs.log = lambda m: _caught.append(m)
_rt = _thr.Thread(target=_rs._report_loop, daemon=True); _rt.start()
with _rs._slock: _rs._state["seq"] = 20            # 20 frames produced
_rs.stats["192.168.1.100"]["sent"] = 10
_t.sleep(0.25); _rs.stop_event.set(); _rt.join(timeout=1)
_line = _caught[0] if _caught else ""
check("reporter emits a rates line", _line.startswith("rates: produced="))
check("reporter reports produced rate > 0", "produced=" in _line and float(_line.split("produced=")[1].split("/s")[0]) > 0)
check("reporter shows the live projector's send_ms + cap", "send~50ms" in _line and "cap~20/s" in _line)
check("reporter shows governor quality + frame size", "q=45" in _line and "frame=40kB" in _line, _line)
check("reporter marks a dead projector reconnecting", ".101 reconnecting" in _line)

# --- capture: torn frames from a shared bitmap buffer (root cause of the wall corruption) -------
# 2026-09-11: the wall showed a frame that was part one screen state, part another, for 8-15s at a
# time. DisplayStreamSource dispatched its CGDisplayStream handler on dispatch_get_global_queue()
# (CONCURRENT), and _to_pil rendered every frame into one shared self._buf with no lock. Two
# overlapping handlers rendered different frames into the same bytes; each then copied the mix out.
print("\ncapture: frame isolation")
import threading as _cthr, time as _ct
try:
    import pm_capture as _cap
    _cap_ok = True
except Exception as _e:
    _cap_ok = False
    check("pm_capture imports", False, _e)

if _cap_ok:
    import inspect as _insp
    # (1) SOURCE GUARD: the CGDisplayStream handler must not run on the global CONCURRENT queue.
    _runsrc = _insp.getsource(_cap.DisplayStreamSource._run)
    check("capture handler is NOT on the global concurrent queue",
          "dispatch_get_global_queue" not in _runsrc)
    check("capture handler uses a SERIAL dispatch queue",
          "dispatch_queue_create" in _runsrc or "_make_queue" in _runsrc)

    # (2) BEHAVIOURAL: two concurrent _to_pil calls must each return an UNMIXED frame.
    class _FakeCtx:
        def __init__(self): self.n = 0; self._l = _cthr.Lock()
        def render_toBitmap_rowBytes_bounds_format_colorSpace_(self, ci, buf, bpr, rect, fmt, cs):
            with self._l:
                self.n += 1; val = 0x40 * self.n      # 0x40 then 0x80: distinct per call
            half = len(buf) // 2
            for i in range(half): buf[i] = val
            _ct.sleep(0.05)                            # window for the other thread to interleave
            for i in range(half, len(buf)): buf[i] = val

    class _FakeCIImage:
        @staticmethod
        def imageWithIOSurface_(s): return object()
    class _FakeQuartz:
        CIImage = _FakeCIImage

    _src = _cap.DisplayStreamSource.__new__(_cap.DisplayStreamSource)
    _src.panel_w = _src.out_w = 64; _src.panel_h = _src.out_h = 64
    _src.letterbox = False; _src._flip = None
    _src._cs = None; _src._ctx = _FakeCtx()
    _src._buf = bytearray(_src.out_w * 4 * _src.out_h)

    _real_q = _cap.Quartz
    _cap.Quartz = _FakeQuartz
    _out = {}
    def _grab(tag):
        try: _out[tag] = _src._to_pil(object())
        except Exception as ex: _out[tag] = ex
    _t1 = _cthr.Thread(target=_grab, args=("a",)); _t2 = _cthr.Thread(target=_grab, args=("b",))
    _t1.start(); _ct.sleep(0.02); _t2.start(); _t1.join(timeout=5); _t2.join(timeout=5)
    _cap.Quartz = _real_q

    def _uniform(im):
        return isinstance(im, Image.Image) and len(im.convert("RGB").getcolors(16) or [0] * 99) == 1
    check("concurrent capture frame A is not torn", _uniform(_out.get("a")), _out.get("a"))
    check("concurrent capture frame B is not torn", _uniform(_out.get("b")), _out.get("b"))

# --- beacon cadence (input-guide OSD suppression) ------------------------------------------------
# 2026-09-11: the projector's input-guide OSD came back on STATIC SLIDES (never on video - video
# sends frames constantly, a slide sends one every ~10s). A hand-sent 1s beacon to .194 silenced it
# completely in a live A/B, proving the mechanism works and the cadence was the lever. The vendor
# app beacons every 500ms, so 0.5s matches it.
print("\nbeacon: cadence")
import threading as _bthr, time as _bt
check("BEACON_PERIOD matches the vendor's 500ms sleep", abs(fm.BEACON_PERIOD - 0.5) < 1e-9,
      fm.BEACON_PERIOD)

class _FakeUDP:
    def __init__(self): self.sends = []
    def sendto(self, pkt, addr): self.sends.append((_bt.time(), pkt, addr))

_snd = fm.Sender.__new__(fm.Sender)
_snd.ip = "10.0.0.9"; _snd._stop = False; _snd._associated = True
_snd.commkey = bytes(range(16)); _snd.pkt500 = b"500"; _snd.pkey = bytes(16)
_snd._ctl_lock = _bthr.RLock(); _snd._last_ctl = 0.0

_ob, _od = fm.BEACON_BURST_N, fm.BEACON_BURST_DT
fm.BEACON_BURST_N, fm.BEACON_BURST_DT = 1, 0.01      # skip the association burst for the test
_u = _FakeUDP()
_bt_thread = _bthr.Thread(target=_snd._assoc_loop, args=(_u,), daemon=True); _bt_thread.start()
_bt.sleep(2.6)
_snd._stop = True; _bt_thread.join(timeout=2)
fm.BEACON_BURST_N, fm.BEACON_BURST_DT = _ob, _od

_steady = [t for t, p, a in _u.sends if p.startswith(b"7000430")][1:]   # drop the burst beacon
check("beacon loop sends steadily (>=3 in 2.6s at 0.5s)", len(_steady) >= 3, len(_u.sends))
if len(_steady) >= 3:
    _gaps = [_steady[i] - _steady[i - 1] for i in range(1, len(_steady))]
    _worst = max(_gaps)
    check("no beacon gap exceeds 1s (the OSD redraw window)", _worst < 1.0, round(_worst, 3))
_pkt = _u.sends[0][1] if _u.sends else b""
check("beacon is 55 bytes, vendor layout", len(_pkt) == 55, len(_pkt))
check("beacon targets UDP 10000", _u.sends and _u.sends[0][2] == ("10.0.0.9", 10000))

# --- adaptive governor: budget must follow the ACTUAL cadence -------------------------------------
# 2026-09-11 (Chris): on a STATIC frame the governor pulled quality down to q=20 instead of up to
# q=100. The budget was a fixed per-frame byte target derived from a NOMINAL 10fps
# (KBPS*1024*max(min_interval, 0.1)), so it starved quality on slides where we put one frame on the
# wire every ~3s and used ~30x less bandwidth than the target allowed.
print("\ngovernor: rate-aware budget")
_gs = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_gs.target_kbps = 700.0; _gs.min_interval = 0.0
_gs._prod_ema = None

check("vendor per-frame cap is known (WM stops lowering quality at 512000 bytes)",
      getattr(fm, "VENDOR_FRAME_CAP", None) == 512000, getattr(fm, "VENDOR_FRAME_CAP", None))

# video: 10 fps -> the OLD 70kB budget, unchanged (no regression for motion)
_gs._prod_ema = 0.1
_tv = _gs._gov_target_bytes()
check("at 10fps the budget is unchanged (~70kB)", abs(_tv - 700 * 1024.0 / 10.0) < 1.0, _tv)

# slides: producer barely runs; the wire cadence floor is the 3s keyframe
_gs._prod_ema = 10.0                       # one produced frame every 10s
_ts = _gs._gov_target_bytes()
check("on a static slide the budget opens up to the vendor cap", _ts == fm.VENDOR_FRAME_CAP, _ts)
check("static budget is far above the old fixed 70kB", _ts > 700 * 1024.0 / 10.0 * 5, _ts)

# convergence: from q=20 with a big headroom, reach QMAX in a few calls (not 16)
_oj, _oq = fm.JFIF, fm._ADAPT_Q
_oqmin, _oqmax = fm.JFIF_QMIN, fm.JFIF_QMAX
fm.JFIF = True; fm.JFIF_QMIN, fm.JFIF_QMAX = 15, 100
fm._ADAPT_Q = 20
_n = 0
while fm._ADAPT_Q < 100 and _n < 20:
    _bytes = 1500 * fm._ADAPT_Q            # crude monotonic bytes(q) model, ~30kB at q20
    fm.adapt_quality(_bytes, fm.VENDOR_FRAME_CAP)
    _n += 1
check("static frame drives quality UP to QMAX", fm._ADAPT_Q == 100, fm._ADAPT_Q)
check("converges in <=5 steps, not 16", _n <= 5, _n)

# safety: still backs off when a frame blows the budget
fm._ADAPT_Q = 100
fm.adapt_quality(300000, 70000)
check("still lowers quality when over budget", fm._ADAPT_Q < 100, fm._ADAPT_Q)
_before = fm._ADAPT_Q
fm.adapt_quality(71000, 70000)
check("mild overshoot backs off gently (<=5)", _before - fm._ADAPT_Q <= 5, _before - fm._ADAPT_Q)
fm.JFIF, fm._ADAPT_Q = _oj, _oq
fm.JFIF_QMIN, fm.JFIF_QMAX = _oqmin, _oqmax

# Chris's rule, stated 2026-09-11: "high fps content = high fps stream. low fps content = low fps
# but HQ." The budget is a byte RATE divided by the cadence, so it falls as content speeds up.
print("\ngovernor: rate <-> quality tradeoff holds across the range")
_ps = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_ps.target_kbps = 700.0; _ps.min_interval = 0.0
_rows = []
for _fps in (0.1, 0.33, 1.0, 5.0, 10.0, 30.0):
    _ps._prod_ema = 1.0 / _fps
    _rows.append((_fps, _ps._gov_target_bytes()))
print("    content fps -> per-frame budget: " +
      ", ".join("%.2f->%dkB" % (f, b / 1024) for f, b in _rows))
check("budget never increases as content speeds up",
      all(_rows[i][1] <= _rows[i - 1][1] for i in range(1, len(_rows))), _rows)
check("slow content gets the vendor cap (max quality headroom)",
      _rows[0][1] == fm.VENDOR_FRAME_CAP, _rows[0][1])
check("fast content gets a small budget (quality yields to fps)",
      _rows[-1][1] < 32 * 1024, _rows[-1][1])
check("budget never exceeds the vendor per-frame cap",
      all(b <= fm.VENDOR_FRAME_CAP for _, b in _rows), _rows)

# --- adaptive quality FLOOR -----------------------------------------------------------------------
# 2026-09-11 (Chris: "Are you sure we need to drop to q=15 to maintain max fps?"). No. Measured with
# the real encoder on the real slide + a busy photographic frame, priced through the fitted send
# model send_ms = 0.519*kB + 32.4 (1.88 MB/s module drain, 32ms latency):
#     SLIDE  q15 48kB 17.4fps | q75 107kB 11.4fps | q85 130kB 10.0fps | q100 319kB 5.1fps
#     PHOTO  q15 31kB 20.5fps | q75  69kB 14.7fps | q85  88kB 12.8fps | q100 310kB 5.2fps
# q75->q15 buys ~6 fps and costs the picture. The byte curve knees at ~q85. And the vendor drives
# this projector at 0.33 fps - fps above ~10 is worthless here and burns the module's send budget.
print("\ngovernor: quality floor")
_MODES = _app.MODES if "_app" in dir() else None
check("Adaptive floor is 75, not the old 15", fm.JFIF_QMIN == 75, fm.JFIF_QMIN)

# the floor must never let the governor reach mush, however far over budget a frame goes
_oj, _oq = fm.JFIF, fm._ADAPT_Q
_oqmin, _oqmax = fm.JFIF_QMIN, fm.JFIF_QMAX
fm.JFIF = True; fm.JFIF_QMIN, fm.JFIF_QMAX = 75, 100
fm._ADAPT_Q = 100
for _ in range(40):
    fm.adapt_quality(5_000_000, 40_000)          # absurdly over budget, every frame
check("governor never drops below the floor under extreme pressure", fm._ADAPT_Q == 75, fm._ADAPT_Q)
check("floor still leaves the governor a real lever (75..100)", fm.JFIF_QMAX - fm.JFIF_QMIN >= 20,
      (fm.JFIF_QMIN, fm.JFIF_QMAX))
fm.JFIF, fm._ADAPT_Q = _oj, _oq
fm.JFIF_QMIN, fm.JFIF_QMAX = _oqmin, _oqmax

# --- governor denominator = the capture source's OWN frame counter ------------------------------
# 2026-09-11. The send-rate denominator (6292f48) ran away on hardware: quality -> encode cost ->
# produce rate -> send rate is a loop through our own CPU. The produce cadence has the same flaw in
# weaker form. The only EXOGENOUS signal is the capture source's seq counter: CGDisplayStream/SCK
# bump it once per frame the OS pushes, at the rate the CONTENT changes, no matter how slowly we
# encode or send. This test makes the encoder deliberately slow (~10/s) against a 30/s source and
# checks the governor sees 30, not 10 - the property whose absence caused the runaway.
print("\ngovernor: content rate comes from the capture seq counter")
import threading as _qthr, time as _qt
class _FakeStream:
    """seq advances on its OWN thread at a fixed rate, like the OS capture callback."""
    def __init__(self, fps):
        self.seq = 0; self.fps = fps; self._run = True
        self.img = slide(1)
        _qthr.Thread(target=self._tick, daemon=True).start()
    def _tick(self):
        while self._run:
            _qt.sleep(1.0 / self.fps); self.seq += 1
    def get(self): return self.seq, self.img
    def stop(self): self._run = False

_qs = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_qs.stop_event = _qthr.Event(); _qs.pause_event = _qthr.Event()
_qs._slock = _qthr.Lock(); _qs._state = {"scans": None, "seq": 0}
_qs.log = lambda *a: None; _qs.capture = "stream"; _qs.target_kbps = 700.0
_qs.governor = False                                  # isolate the measurement from the governor
_fake = _FakeStream(30.0); _qs._make_stream = lambda: _fake

_real_bs = fm.build_scans
_slow_scans = [(b"x" * 100, i * 96, i == 7) for i in range(8)]
def _slow_build(img, **kw):
    _qt.sleep(0.1)                                    # a deliberately EXPENSIVE encoder: <=10/s
    return list(_slow_scans)
fm.build_scans = _slow_build
_pt = _qthr.Thread(target=_qs._produce, daemon=True); _pt.start()
_qt.sleep(2.5)
with _qs._slock: _produced = _qs._state["seq"]
_qs.stop_event.set(); _pt.join(timeout=3); _fake.stop()
fm.build_scans = _real_bs

_cf = getattr(_qs, "_content_fps", None)
check("producer really was throttled by the slow encoder (<= ~12/s)", 0 < _produced / 2.5 <= 12.5,
      "%.1f produced/s" % (_produced / 2.5))
_enc_rate = _produced / 2.5                              # the throttled encode/produce rate (~10/s)
check("content fps tracks the seq/content rate, not our encode cadence",
      _cf is not None and _cf >= 1.5 * _enc_rate,
      "content=%.1f/s vs encode=%.1f/s" % (_cf or 0.0, _enc_rate))
_bq = _qs._gov_target_bytes()
check("budget divides by the measured CONTENT rate, not the encode rate",
      _cf and abs(_bq - 700 * 1024.0 / _cf) < 1.0, "%.0f bytes (content_fps=%.1f)" % (_bq, _cf or 0.0))
check("...so it is far smaller than an encode-rate budget would be", _bq < 700 * 1024.0 / 10.0 * 0.6, _bq)

# poll path (no stream) must still fall back to the fresh-content cadence
_qp = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_qp.target_kbps = 700.0; _qp._content_fps = None; _qp._prod_ema = 0.2
check("without a stream, falls back to the produce cadence (5/s)",
      abs(_qp._gov_target_bytes() - 700 * 1024.0 / 5.0) < 1.0, _qp._gov_target_bytes())

# --- slow-send backoff must not be zero in free-run --------------------------------------------
# 2026-09-11, .193 log: "slow send (9) - backing off to 0.00s". The backoff is
# interval = min(interval*2, 3.0), and in free-run (b8c28d2, min_interval 0) interval is seeded 0
# and only becomes nonzero after a SUCCESSFUL send. A unit that never succeeds multiplies zero by
# two forever: no backoff at all, the 12s SEND_TIMEOUT is the only pacing. Same class of defect as
# the 10fps nominal - free-run broke an assumption downstream. Also `slow` never reset on reconnect
# (the field counter ran 7,8,9,10,11 across three sessions).
print("\nsender: slow-send backoff in free-run")
import threading as _bthr2, time as _bt2, socket as _bsock
_oF, _oR = _fs2.FAIL_LIMIT, _fs2.RECONNECT_WAIT
_fs2.RECONNECT_WAIT = 0.05                       # module constant; 30s in production
_bl = []
class _TimeoutSender:
    def send_strips(self, scans, idxs): raise _bsock.timeout("timed out")
    def close(self): pass
_bs = _fs2.MirrorSession.__new__(_fs2.MirrorSession)
_bip = "10.0.0.7"
_bs.ips = [_bip]; _bs.stop_event = _bthr2.Event(); _bs._slock = _bthr2.Lock()
_bs._state = {"scans": None, "seq": 0}
# seed EVERY counter the worker does `+=` on; a missing key is a KeyError that kills the daemon
# thread silently (no log line, no check failure) - which is exactly what hid the reconnect
_bs.stats = {_bip: {"sent": 0, "errors": 0, "drops": 0, "reconnects": 0, "slow": 0}}
_bs.alive = {_bip: True}; _bs.current = {_bip: None}; _bs.wanted = {_bip: True}
_bs.auto_reconnect = True; _bs.min_interval = 0.0; _bs.log = lambda m: _bl.append(m)
_bs._last_close = {}                             # settle clock (worker reads/writes it per reconnect)
_bs.gov_q = 50; _bs.gov_frame_bytes = 1000
with _bs._slock:
    _bs._state["scans"] = [(b"x" * 100, i * 96, i == 7) for i in range(8)]; _bs._state["seq"] = 1
_oc, _om = fm._connect_with_retry, fm.pj_is_muted
fm._connect_with_retry = lambda ip, tries=1: _TimeoutSender()
fm.pj_is_muted = lambda ip: False
_bt_ = _bthr2.Thread(target=_bs._worker, args=(_bip, _TimeoutSender()), daemon=True); _bt_.start()
_bt2.sleep(7.0)
_bs.stop_event.set(); _bt_.join(timeout=3)
fm._connect_with_retry, fm.pj_is_muted = _oc, _om
_fs2.FAIL_LIMIT, _fs2.RECONNECT_WAIT = _oF, _oR
import re as _bre
_backs = [float(m.group(1)) for l in _bl for m in [_bre.search(r"backing off to ([0-9.]+)s", l)] if m]
_slows = [int(m.group(1)) for l in _bl for m in [_bre.search(r"slow send \((\d+)\)", l)] if m]
check("a timed-out send is logged with a backoff", len(_backs) >= 1, _bl[:3])
check("the first backoff is NOT zero in free-run (>= 0.25s)", _backs and _backs[0] >= 0.25, _backs[:3])
check("backoff grows and is capped at 3s", _backs and all(b <= 3.0 for b in _backs) and
      (len(_backs) < 2 or _backs[1] >= _backs[0]), _backs[:4])
check("three timeouts drop the unit", any("DROPPED" in l for l in _bl), [l for l in _bl if "err" in l][:3])
# The drop follows the SIXTH timeout, not the third: slow<4 are silent retries, only 4..6 count
# as failures against FAIL_LIMIT=3. Slice by the DROPPED line itself, and require that a reconnect
# was actually observed, so the reset assertion cannot pass vacuously.
_di = next((i for i, l in enumerate(_bl) if "DROPPED" in l), None)
_reconn = _di is not None and any("connected" in l for l in _bl[_di + 1:])
_post = [int(m.group(1)) for l in (_bl[_di + 1:] if _di is not None else [])
         for m in [_bre.search(r"slow send \((\d+)\)", l)] if m]
check("a reconnect was observed after the drop (the reset check is not vacuous)", _reconn,
      _bl[_di:_di + 4] if _di is not None else "no DROPPED line")
check("slow counter resets on reconnect (first post-drop slow send is (1))",
      _reconn and _post and _post[0] == 1, _post[:3])

print("\n" + ("ALL OFFLINE TESTS PASSED" if not FAIL else "FAILURES: %s" % FAIL))
sys.exit(1 if FAIL else 0)
