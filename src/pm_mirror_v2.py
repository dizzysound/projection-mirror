#!/usr/bin/env python3
"""
pm_mirror_v2.py - PT-F300 live sender: discovery, association, encryption, JPEG strips, PJLink.

Protocol:
  pkey (per projector): AES-128 key derived from the projector's LOCATION field (see pm_pkey.py);
                        the UDP-500 payload == AES_enc_ECB(pkey, bswap32(pkey))
  m_CommKey (image)   : bswap32( AES_dec_ECB(pkey, 501blob) )   [501blob = 16B after "50103500 1"]
  strip payload       : AES-128-ECB(m_CommKey) over (len//16)*16 bytes, last len%16 bytes PLAINTEXT
  TCP-12000           : plaintext ASCII  INIT 07 RT <w> <h> <token> / STAT RES / TYPE TCP none none
                        replies 104 OK 0 TCP / 112 <w> <h> / 120 OK <dataport>

Each projector's key is derived on demand from its discovery reply, so no per-unit setup is needed.

Usage:
  python3 pm_mirror_v2.py 192.168.1.50 jpeg IMG   # send IMG as encrypted full-JPEG strips
"""
import socket, struct, io, time, threading, sys, hashlib, zlib
try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("pip3 install pycryptodome")

PJLINK_PW = "panasonic"

# ---- per-projector pkey = key derived from the projector LOCATION; CONSTANT per projector ----
# Optional cache keyed by IP; empty is fine (pkey_for() derives each key on demand from discovery).
# The two entries below are example vectors for the common LOCATION values "RIGHT" / "LEFT".
PKEYS = {
    "192.168.1.101": "45dd79332116f723c156db7c9ef076d7",   # LOCATION "RIGHT"
    "192.168.1.100": "c41eebe7bac4340ad510533592347e64",   # LOCATION "LEFT"
}
TOKEN  = "FEA8E189"
# 700 discovery/assoc packet: "7000430"+TOKEN+<32B blob>+"00000000". Blob not auth-checked (auth is the
# 500/pkey); reused across projectors. Discovery wake ("600TRANSPIC0430"+TOKEN broadcast) is in connect().
PKT700 = bytes.fromhex("3730303034333046454138453138393cfb93911047e3d6c2d87e9f09a0bb7"
                       "01dc09b2536dae136971883a6bd537eed3030303030303030")

def bswap32(b): return b"".join(b[i:i+4][::-1] for i in range(0,len(b),4))

def make_pkt500(pkey):
    return b"500043001"+AES.new(pkey,AES.MODE_ECB).encrypt(bswap32(pkey))

def _pj_readline(s, limit=512):
    """One CR-terminated PJLink line. TCP has no message boundaries, so a single
    recv() can return half a line or two lines at once; the greeting seed in
    particular must not be read short or the MD5 digest comes out wrong."""
    buf = b""
    while b"\r" not in buf:
        if len(buf) >= limit: break
        chunk = s.recv(limit - len(buf))
        if not chunk: break
        buf += chunk
    return buf.split(b"\r")[0].decode("ascii", "replace").strip()

def pjlink(ip, cmd):
    s=socket.socket(); s.settimeout(6)
    try:
        s.connect((ip,4352))
        h=_pj_readline(s).split(" ")
        m=(hashlib.md5((h[2]+PJLINK_PW).encode()).hexdigest()+cmd+"\r") if len(h)>=3 and h[1]=="1" else cmd+"\r"
        s.sendall(m.encode())
        return _pj_readline(s)
    finally:
        try: s.close()
        except Exception: pass

# PJLink %1POWR states (spec v1.04 s4.2). The two transition states are the whole
# reason this is a state machine and not a boolean: a power command sent during
# either one is answered ERR3 "unavailable time" (s4.1) and does nothing.
PJ_STANDBY, PJ_ON, PJ_COOLING, PJ_WARMUP = "0", "1", "2", "3"

def pj_power_state(ip):
    """'0' standby / '1' on / '2' cooling / '3' warming, or None if unreachable."""
    return pj_get(ip, "%1POWR ?")

def power_on_network(ip, timeout=240, log=None):
    """Bring the projector up and onto the NETWORK input. Returns True on success.

    Cooling is the case that used to break this. A projector that is cooling
    reports '2' and refuses %1POWR 1 with ERR3; when it finishes it settles at
    '0' STANDBY, never at '1'. The old code issued one power-on into the ERR3
    window, discarded the error, then waited 100s for a '1' that cannot arrive
    and mirrored to a projector that was still switched off. Wait the cooling
    out, then power on from standby."""
    say = log or (lambda m: print("[%s] %s" % (ip, m)))
    deadline = time.monotonic() + timeout
    announced = set()
    while time.monotonic() < deadline:
        st = pj_power_state(ip)
        if st is None:
            time.sleep(4); continue
        if st == PJ_ON:
            break
        if st in (PJ_COOLING, PJ_WARMUP):
            if st not in announced:
                announced.add(st)
                say("projector is %s; waiting for it to settle before powering on"
                    % ("cooling down" if st == PJ_COOLING else "warming up"))
            time.sleep(4); continue
        # standby: safe to power on now
        r = pjlink(ip, "%1POWR 1")
        if r.upper().endswith("ERR3"):
            time.sleep(4); continue          # raced a transition; re-read and wait
        time.sleep(4)
    else:
        say("timed out after %ds waiting for power on (last state %r)" % (timeout, pj_power_state(ip)))
        return False

    while time.monotonic() < deadline:
        if pjlink(ip, "%1INPT 51").endswith("=OK"):
            return True
        time.sleep(4)
    say("powered on but could not select the NETWORK input within %ds" % timeout)
    return False

def pj_blank(ip):
    """PJLink AV-mute ON: hardware-blank the panel (survives WM session teardown)."""
    try: return pjlink(ip,"%1AVMT 31")
    except Exception as e: return "err %s"%e
def pj_unblank(ip):
    """PJLink AV-mute OFF: restore the picture (call on start)."""
    try: return pjlink(ip,"%1AVMT 30")
    except Exception as e: return "err %s"%e

try:
    import pm_pkey            # top-level so PyInstaller bundles it
except Exception:
    pm_pkey = None

DISCOVERY_PKT = b"600TRANSPIC0430" + TOKEN.encode()

def _parse_602(data):
    """Field offsets confirmed by protocol analysis of the projectors' 602 discovery replies."""
    if not data.startswith(b"602") or len(data) < 62:
        return None
    return {"model":    data[3:22].decode("ascii", "replace").strip(),
            "location": data[38:54].decode("ascii", "replace"),   # "RIGHT"/"LEFT" padded
            "name":     data[54:62].decode("ascii", "replace").strip(),
            "raw": data}

def _bcast_of(ip):
    return ".".join(str(ip).split(".")[:3] + ["255"])

def _local_ipv4():
    """This machine's LAN address, for the broadcast address. No packet is sent by a UDP connect."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))       # TEST-NET-1: routed nowhere, just picks the interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()

def discover_602(ip, timeout=3.0):
    """Send the UDP-10000 600 discovery and parse this projector's 602 reply."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    try: s.bind(("", 0))
    except OSError: pass
    t0 = time.time()
    try:
        while time.time() - t0 < timeout:
            for tgt in (ip, _bcast_of(ip)):
                try: s.sendto(DISCOVERY_PKT, (tgt, 10000))
                except OSError: pass
            try: data, addr = s.recvfrom(512)
            except socket.timeout: continue
            if addr[0] != ip: continue
            info = _parse_602(data)
            if info: return info
    finally:
        s.close()
    return None

def discover_all(timeout=3.0, hint_ips=()):
    """Broadcast the 600 discovery and collect EVERY 602 reply on the subnet.

    This is the "Find Projectors" sweep. It is discovery only - the same UDP-10000 packet the
    wake step already broadcasts - so it does NOT open a session and cannot wedge a projector's
    network module (that takes rapid ASSOCIATION cycling, see the 2026-09-08 checkpoint).
    Returns a list of 602 dicts, each with an added "ip".
    """
    targets = set()
    for ip in list(hint_ips) + [x for x in (_local_ipv4(),) if x]:
        targets.add(_bcast_of(ip))
    if not targets:
        targets.add("255.255.255.255")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.4)
    try: s.bind(("", 0))
    except OSError: pass
    found, t0, next_send = {}, time.time(), 0.0
    try:
        while time.time() - t0 < timeout:
            if time.time() >= next_send:      # re-send: a single broadcast is easy to miss
                for tgt in sorted(targets):
                    try: s.sendto(DISCOVERY_PKT, (tgt, 10000))
                    except OSError: pass
                next_send = time.time() + 1.0
            try: data, addr = s.recvfrom(512)
            except socket.timeout: continue
            except OSError: break
            info = _parse_602(data)
            if info and addr[0] not in found:
                info["ip"] = addr[0]
                found[addr[0]] = info
    finally:
        s.close()
    return [found[k] for k in sorted(found)]

def web_url(ip):
    """The projector's own WEB control page - what WM's "WEB control" opens."""
    return "http://%s/" % ip

def remote_url(ip, lang="en"):
    """The projector's browser remote. WM's "Remote control" is not a protocol at all: it opens
    this CGI in the default browser (http://%@/cgi-bin/remocon.cgi?lang=%@ in the WM binary)."""
    return "http://%s/cgi-bin/remocon.cgi?lang=%s" % (ip, lang)

def pkey_for(ip):
    """The projector's association key: baked constant if known, otherwise DERIVED from its
    602 location field via the key derivation (see pm_pkey.py)."""
    if ip in PKEYS:
        return bytes.fromhex(PKEYS[ip])
    if pm_pkey is None:
        raise RuntimeError("pm_pkey module missing; cannot derive a key for %s" % ip)
    info = discover_602(ip)
    if not info:
        raise RuntimeError("no 602 discovery reply from %s; cannot derive its key" % ip)
    k = pm_pkey.create_temp_key(info["location"])
    print("[%s] derived pkey from location %r (%s): %s"
          % (ip, info["location"].strip(), info["name"], k.hex()))
    return k

def pj_get(ip, cmd):
    """Value after '=' for a PJLink query, or None if unreachable / ERRn."""
    try:
        r = pjlink(ip, cmd)
        if "=" not in r: return None
        v = r.split("=")[-1].strip()
        return None if v.upper().startswith("ERR") else v
    except Exception:
        return None

def pj_state(ip):
    """Current power / input / AV-mute. Read this BEFORE applying a command."""
    return {"power": pj_get(ip, "%1POWR ?"),
            "input": pj_get(ip, "%1INPT ?"),
            "mute":  pj_get(ip, "%1AVMT ?")}

MUTE_ON = ("11", "21", "31")     # PJLink AVMT: 11 video, 21 audio, 31 both; 30 = off
def pj_is_muted(ip):
    v = pj_get(ip, "%1AVMT ?")
    return None if v is None else (v in MUTE_ON)

def _destuff(b):
    o=bytearray(); k=0
    while k<len(b):
        o.append(b[k]); k+=2 if (b[k]==0xFF and k+1<len(b) and b[k+1]==0x00) else 1
    return bytes(o)

import os
# Sender-side color correction (NETWORK input has no projector picture mode).
# Tune live via env, e.g.:  WB_B=0.90 WB_R=1.03 GAMMA=1.0 python3 pm_mirror_v2.py ...
CC = {"r":float(os.environ.get("WB_R",1.0)),
      "g":float(os.environ.get("WB_G",1.0)),
      "b":float(os.environ.get("WB_B",0.90)),      # neutralize projector's cool cast (grays go neutral)
      "gamma":float(os.environ.get("GAMMA",0.8)),  # projector lifts mids on NETWORK input; pre-darken
      "knee":float(os.environ.get("KNEE",255)),    # highlight rolloff start (input level)
      "ceil":float(os.environ.get("CEIL",255))}    # max output (compress knee..255 -> knee..ceil)
_CC_LUT=None
import threading as _threading
_PARAM_LOCK=_threading.RLock()   # guards CC/QUALITY/FIT/_CC_LUT across the live thread + GUI thread
def _identity_cc():
    return (CC["r"]==1.0 and CC["g"]==1.0 and CC["b"]==1.0 and CC["gamma"]==1.0
            and CC["knee"]>=255 and CC["ceil"]>=255)
def _build_luts():
    g=CC["gamma"]; knee=CC["knee"]; ceil=CC["ceil"]
    def lut(gain):
        out=bytearray(256)
        for i in range(256):
            v=(((i/255.0)**(1.0/g))*gain)*255.0
            if knee<255 and v>knee:                 # soft highlight rolloff
                v=knee+(v-knee)*((ceil-knee)/(255.0-knee))
            out[i]=min(255,max(0,int(v+0.5)))
        return bytes(out)
    return (lut(CC["r"]),lut(CC["g"]),lut(CC["b"]))
def color_adjust(img):
    global _CC_LUT
    with _PARAM_LOCK:
        if _identity_cc():
            return img
        lut=_CC_LUT
        if lut is None: lut=_CC_LUT=_build_luts()
    from PIL import Image
    r,gc,b=img.split()
    return Image.merge("RGB",(r.point(lut[0]), gc.point(lut[1]), b.point(lut[2])))

def encode_scan(band, quality=50, jfif=False):
    """1024xH RGB -> JPEG for one strip.
    jfif=False (proven default): marker-less stuffed baseline scan (ends FFD9); the projector uses
      its built-in q50 Annex-K tables, so quality MUST be 50.
    jfif=True (experimental): the FULL JFIF (FFD8..FFD9, DQT embedded) as WM-Android sends, so ANY
      quality decodes - enables adaptive quality as a bytes governor."""
    bio=io.BytesIO(); band.save(bio,"JPEG",quality=quality,subsampling=(SUBSAMPLING if jfif else 2),optimize=False)
    j=bio.getvalue()
    if jfif:
        return j                      # whole JFIF; quant tables travel with the frame
    s=j.find(b"\xff\xda"); ln=struct.unpack(">H",j[s+2:s+4])[0]
    return j[s+2+ln:]   # keep FF00 stuffing: projector expects standard stuffed JPEG scan

FIT       = os.environ.get("FIT","letterbox")        # "letterbox" (keep aspect, black bars) or "stretch"
QUALITY   = int(os.environ.get("QUALITY","50"))      # JPEG quality (projector built-in tables are q50)
STRIP_DELAY = float(os.environ.get("STRIP_DELAY","0"))  # inter-strip pause (s); 0 = coalesced single send
# Control plane. Default = exactly what the vendor apps send (measured 2026-09-09, all 3 platforms):
# NOOP on a ~4 s timer only, NO UDP-12004 datagram (no vendor app ever sends one), and the 700 beacon
# as a short burst at association then ~10 s cadence (700 only; 500 is connect-time only). Our old per-frame NOOP + 12004 pulse + 100 ms
# 700/500/600 blast is kept behind CTL_LEGACY=1 for A/B only - it is the prime wedge suspect.
CTL_LEGACY = os.environ.get("CTL_LEGACY","0") == "1"
BEACON_BURST_N  = int(os.environ.get("BEACON_BURST_N","10"))    # 700 beacons during association
BEACON_BURST_DT = float(os.environ.get("BEACON_BURST_DT","0.36"))
# Steady post-association beacon. It MUST be built with the LIVE session key (build_beacon): the
# projector decrypts the 32-byte username field with m_CommKey to confirm we own the NETWORK input
# and suppress the input-guide OSD. A STATIC replayed beacon fails that check - OSD toggles (valid
# beacon @10 s) or stays on (no beacon). 0 disables (diagnostic only). TCP NOOP ~4 s keeps the session.
# The vendor app beacons every 500 ms, not the ~10 s an earlier estimate suggested. 10 s left a
# visible gap and 2 s still did - the
# input-guide OSD was back on slides 2026-09-11. A hand-sent 1 s beacon silenced it completely in a
# live A/B on one projector, so the mechanism is sound and the cadence was the lever. Match the vendor at 0.5 s.
BEACON_PERIOD   = float(os.environ.get("BEACON_PERIOD","0.5"))
BEACON_USER     = os.environ.get("BEACON_USER","no name")     # WM default; shown in the user list

def _beacon_packet(commkey, token, user):
    """The session-keyed 700 beacon bytes. Shared by the in-process sender and the subprocess."""
    if isinstance(user, str): user = user.encode("ascii", "replace")
    if isinstance(token, str): token = token.encode()
    u = user[:32].ljust(32, b" ")
    return b"7000430" + token + AES.new(commkey, AES.MODE_ECB).encrypt(u) + b"00000000"

def beacon_hold(ip, commkey_hex, token, user, period):
    """Steady beacon in its OWN process, so the encoder's CPU/GIL load can never starve its
    cadence (the in-process beacon stretched 0.5s->10s under a pegged core, and the input-guide
    OSD returned). Sends every `period` on an absolute schedule until the parent app exits."""
    beacon = _beacon_packet(bytes.fromhex(commkey_hex), token, user)
    us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ppid = os.getppid()
    next_t = time.monotonic()
    while True:
        if os.getppid() != ppid:                 # parent died (reparented to launchd) -> don't orphan
            return
        try: us.sendto(beacon, (ip, 10000))
        except OSError: pass
        next_t += period
        dt = next_t - time.monotonic()
        if dt > 0: time.sleep(dt)
        else: next_t = time.monotonic()          # fell behind: resync rather than burst-catch-up
VIDEO_SNDBUF    = int(os.environ.get("VIDEO_SNDBUF","65536"))  # cap the video socket send buffer so
                                                              # ~1 frame is in flight (low latency at
                                                              # free-run); 0 = OS default. sendall then
                                                              # blocks on the MODULE, not the kernel buffer.
# EXPERIMENTAL, default OFF. Marker-less (JFIF off) is the only hardware-proven path and stays
# default. In JFIF mode each strip carries its own DQT, so any quality decodes -> the byte lever
# for higher sustained fps (full JFIF carries its own quant tables, so any quality decodes).
JFIF        = os.environ.get("JFIF","0") == "1"
JFIF_QMIN   = int(os.environ.get("JFIF_QMIN","75"))   # see MODES in pm_app.py for why 75, not 15
JFIF_QMAX   = int(os.environ.get("JFIF_QMAX","85"))
TARGET_KBPS = float(os.environ.get("TARGET_KBPS","0"))   # >0: governor holds this sent-byte rate (JFIF only)
_ADAPT_Q    = 60                                         # current adaptive quality (JFIF governor state)
# --- quality knobs (2026-09-10). fps is projector-latency-capped, not bandwidth, so we can spend
# the encode/display buffer on quality. QPIN: pin JFIF quality (1..100), overriding the mode band +
# governor; None=adaptive -- the usable lever (higher luma q cuts the 8x8 DCT block artifacts).
# SUBSAMPLING: JPEG chroma sampling (0=4:4:4, 1=4:2:2, 2=4:2:0). HARDWARE RESULT 2026-09-10: the
# F300U decoder is 4:2:0-ONLY -- s=0 renders a full-screen checkerboard (MCU-geometry mismatch), so
# the 4:2:0 color macroblocking is the hardware floor. Left as an env-only knob for headless A/B;
# never expose it to the live wall (default stays 2).
SUBSAMPLING = int(os.environ.get("SUBSAMPLING", "2"))
QPIN        = (int(os.environ["QPIN"]) if os.environ.get("QPIN") else None)
SEND_TIMEOUT = float(os.environ.get("SEND_TIMEOUT","12"))  # v1.1: was 4s. sendall blocks until the
    # projector accepts; a full 32KB frame on a busy module can legitimately exceed 4s. Giving up
    # and closing mid-frame is what wedged BOTH projectors on 2026-09-08.

def set_color(quality=None, gamma=None, r=None, g=None, b=None, knee=None, ceil=None, fit=None):
    """Live-update picture settings (rebuilds the color LUT on next frame)."""
    global _CC_LUT, QUALITY, FIT
    with _PARAM_LOCK:
        if quality is not None: QUALITY=int(quality)
        if gamma  is not None: CC["gamma"]=float(gamma)
        if r      is not None: CC["r"]=float(r)
        if g      is not None: CC["g"]=float(g)
        if b      is not None: CC["b"]=float(b)
        if knee   is not None: CC["knee"]=float(knee)
        if ceil   is not None: CC["ceil"]=float(ceil)
        if fit    is not None: FIT=str(fit)
        _CC_LUT=None

def get_color():
    return {"quality":QUALITY,"gamma":CC["gamma"],"r":CC["r"],"g":CC["g"],"b":CC["b"],
            "knee":CC["knee"],"ceil":CC["ceil"],"fit":FIT}

PANEL_W, PANEL_H = 1024, 768   # negotiated per session from the STAT RES reply ("112 <w> <h>")
STRIP_H = 96                   # F300 uses 8 x 96; other panels get a remainder strip

def set_panel(w, h):
    """Adopt a projector-reported resolution. build_scans/fit_panel follow this."""
    global PANEL_W, PANEL_H
    with _PARAM_LOCK:
        if (w, h) != (PANEL_W, PANEL_H):
            print("panel geometry now %dx%d (was %dx%d)" % (w, h, PANEL_W, PANEL_H))
        PANEL_W, PANEL_H = int(w), int(h)

def strip_layout(panel_h, strip_h=None):
    """[(yoff, height)] covering panel_h. 768 -> exactly 8x96, unchanged from the verified path;
    a height that is not a multiple of strip_h gets a shorter final strip."""
    sh = strip_h or STRIP_H
    out, y = [], 0
    while y < panel_h:
        out.append((y, min(sh, panel_h - y))); y += min(sh, panel_h - y)
    return out

def adopt_panel(senders, log=print):
    """Set the shared geometry from connected projectors. One encode is shared by every
    projector, so mixed resolutions cannot be served from one stream - we warn and use the first."""
    res = [(s.panel_w, s.panel_h) for s in senders if getattr(s, "panel_w", None)]
    if not res:
        return (PANEL_W, PANEL_H)
    first = res[0]
    if any(r != first for r in res):
        log("WARNING: projectors report different resolutions %s; encoding for %dx%d only"
            % (res, first[0], first[1]))
    set_panel(*first)
    return first

def fit_panel(img, w=None, h=None):
    from PIL import Image
    w = w or PANEL_W; h = h or PANEL_H
    img = img.convert("RGB")
    if FIT == "stretch":
        return img.resize((w, h), Image.BILINEAR)
    iw, ih = img.size
    sc = min(float(w) / iw, float(h) / ih)
    nw, nh = max(1, int(iw * sc)), max(1, int(ih * sc))
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(img.resize((nw, nh), Image.BILINEAR), ((w - nw) // 2, (h - nh) // 2))
    return canvas

def fit_1024x768(img):
    from PIL import Image
    img=img.convert("RGB")
    if FIT=="stretch":
        return img.resize((1024,768), Image.BILINEAR)
    iw,ih=img.size
    sc=min(1024.0/iw, 768.0/ih)
    nw,nh=max(1,int(iw*sc)),max(1,int(ih*sc))
    canvas=Image.new("RGB",(1024,768),(0,0,0))
    canvas.paste(img.resize((nw,nh), Image.BILINEAR), ((1024-nw)//2,(768-nh)//2))
    return canvas

def build_scans(img, quality=None):
    """Encode a frame ONCE into (scan, yoff, last, strip_h) tuples, shared across projectors.
    Marker-less mode: quality LOCKED at 50 (projector built-in tables). JFIF mode: full-JFIF strips
    at the adaptive governor quality (or `quality` if given)."""
    w, h = PANEL_W, PANEL_H
    img=color_adjust(fit_panel(img, w, h))
    layout = strip_layout(h)
    if not JFIF:
        return [(encode_scan(img.crop((0,y,w,y+sh)), 50), y, i==len(layout)-1, sh)
                for i,(y,sh) in enumerate(layout)]
    if quality is not None:
        q = max(JFIF_QMIN, min(JFIF_QMAX, int(quality)))
    elif QPIN is not None:
        q = max(1, min(100, int(QPIN)))          # explicit pin overrides the mode band + governor
    else:
        q = max(JFIF_QMIN, min(JFIF_QMAX, _ADAPT_Q))
    return [(encode_scan(img.crop((0,y,w,y+sh)), q, jfif=True), y, i==len(layout)-1, sh)
            for i,(y,sh) in enumerate(layout)]

# Vendor-observed per-frame ceiling: quality is lowered until the JPEG is under 512000
# bytes. No budget we compute may exceed it, however much headroom the cadence appears to give.
VENDOR_FRAME_CAP = 512000

# Empirical send model (validated 2026-09): the projector accepts video at ~1.9 MB/s with a fixed
# ~32 ms per-send cost, so a frame of N bytes takes DRAIN_S_PER_BYTE*N + DRAIN_LATENCY_S to leave
# the wire. It floors the free-run send interval. A frame small enough to fit inside the socket
# send buffer lets sendall() return BEFORE the projector has drained it, so sendall's measured
# duration under-reports the true drain and the worker over-sends - piling slides in the projector's
# decode FIFO, which surfaces as accumulating display lag on rapid Adaptive slide changes (Adaptive
# shrinks frames under load; HQ frames stay >SNDBUF and block in sendall, so they never hit this).
# Pacing by the model bounds the worker to the projector's real intake regardless of buffering; for
# a large frame the model is <= what sendall already measures, so the floor is a no-op there.
DRAIN_S_PER_BYTE = 0.519e-3 / 1024.0   # 0.519 ms per KB  (~1.9 MB/s)
DRAIN_LATENCY_S  = 0.0324              # 32.4 ms fixed per-send cost

def modeled_drain_s(nbytes):
    """Modeled time for `nbytes` of video strips to drain to the projector."""
    return DRAIN_S_PER_BYTE * float(nbytes) + DRAIN_LATENCY_S

GOV_STEP_MIN = 5      # gentle nudge near the target (the original fixed step)
GOV_STEP_MAX = 40     # far from the target, move properly instead of crawling 5 at a time

def _gov_step(excess):
    """Step size for a governor correction. `excess` >= 1 says how far off budget we are.
    5 at the boundary, proportional beyond it, capped so one bad frame cannot slam the quality."""
    return int(min(GOV_STEP_MAX, max(GOV_STEP_MIN, round(GOV_STEP_MIN * float(excess)))))

def adapt_quality(last_frame_bytes, target_frame_bytes):
    """Nudge JFIF adaptive quality toward a per-frame byte budget (the bytes governor).

    Steps are PROPORTIONAL to the miss (2026-09-11). The old fixed +/-5 took 16 frames to climb from
    q20 to q100 - on a static slide the producer runs ~0.1/s, so that was nearly three minutes of
    visibly poor slides. Proportional steps converge in about four, and the caller re-encodes
    immediately after a change rather than waiting for the next screen update."""
    global _ADAPT_Q
    if not JFIF or target_frame_bytes <= 0 or last_frame_bytes <= 0:
        return _ADAPT_Q
    # Pull the state inside the current band FIRST. A mode switch can raise the floor above where
    # the governor happens to be sitting, and the correction branches below are guarded on
    # _ADAPT_Q > QMIN / < QMAX - so an out-of-band value would otherwise stick there forever.
    if _ADAPT_Q < JFIF_QMIN: _ADAPT_Q = JFIF_QMIN
    elif _ADAPT_Q > JFIF_QMAX: _ADAPT_Q = JFIF_QMAX
    ratio = last_frame_bytes / float(target_frame_bytes)
    if ratio > 1.0 and _ADAPT_Q > JFIF_QMIN:
        _ADAPT_Q = max(JFIF_QMIN, _ADAPT_Q - _gov_step(ratio))
    elif ratio < 0.7 and _ADAPT_Q < JFIF_QMAX:
        _ADAPT_Q = min(JFIF_QMAX, _ADAPT_Q + _gov_step(0.7 / max(ratio, 1e-6)))
    return _ADAPT_Q

class Sender:
    panel_w, panel_h = PANEL_W, PANEL_H   # updated from the 112 reply in connect()
    def __init__(self, ip):
        self.ip=ip; self._stop=False; self.commkey=None; self._associated=False
        self.pkey=pkey_for(ip); self.pkt500=make_pkt500(self.pkey)
        self._ctl_lock=threading.RLock(); self._last_ctl=0.0; self._beacon_proc=None
    def build_beacon(self):
        """Session-keyed presence beacon: '7000430'+uid + AES-ECB(m_CommKey, user.ljust(32)) + '00000000'.
        The projector validates the encrypted username with the SESSION key to keep the NETWORK
        input live and the input-guide OSD suppressed. Rebuilt per session, never replayed static."""
        return _beacon_packet(self.commkey, TOKEN, BEACON_USER)
    def _assoc_loop(self, us):
        p600=b"600TRANSPIC0430\x00"+b"\x00"*114
        def send(p):
            try: us.sendto(p,(self.ip,10000))
            except OSError: pass
        # pre-association (or legacy): the 700/500/600 blast every 100 ms until the 501 arrives
        while not self._stop and (CTL_LEGACY or not self._associated):
            for p in (PKT700, self.pkt500, p600): send(p)
            time.sleep(0.1)
        # post-association: burst then steady, both with the VALID session-keyed beacon.
        beacon=self.build_beacon()
        print("[%s] beacon: association done, steady period %.2fs, burst %d x %.2fs"
              % (self.ip, BEACON_PERIOD, BEACON_BURST_N, BEACON_BURST_DT), flush=True)
        for _ in range(BEACON_BURST_N):
            if self._stop: return
            send(beacon); time.sleep(BEACON_BURST_DT)
        bp=getattr(self,"_beacon_proc",None)
        if bp is not None and bp.poll() is None:
            print("[%s] beacon: steady cadence handed to subprocess pid %d" % (self.ip, bp.pid), flush=True)
            return                                # subprocess owns the steady beacon; in-process loop below is the fallback
        # DIAGNOSTIC (2026-09-11): the input-guide OSD came back with irregular ~11-17s gaps between
        # dismissals, which a 2s loop should not produce. Report the ACTUAL achieved cadence and any
        # sendto errors so "we never sent it" is distinguishable from "the projector ignored it".
        n=0; t_win=time.time(); errs=0; worst=0.0; t_prev=time.time()
        while not self._stop:
            if BEACON_PERIOD <= 0:
                time.sleep(0.25); continue
            t=time.time()
            step=min(0.05, BEACON_PERIOD/4.0)      # 0.25 steps cannot pace a 0.5s period
            while not self._stop and time.time()-t < BEACON_PERIOD: time.sleep(step)
            if self._stop: return
            try:
                us.sendto(beacon,(self.ip,10000))
            except OSError as e:
                errs+=1
                if errs<=3: print("[%s] beacon sendto FAILED: %s" % (self.ip, e), flush=True)
            now=time.time(); gap=now-t_prev; t_prev=now
            if gap>worst: worst=gap
            n+=1
            if n%15==0:
                print("[%s] beacon: %d sent, avg %.2fs, worst %.2fs, errs %d"
                      % (self.ip, n, (now-t_win)/15.0, worst, errs), flush=True)
                t_win=now; worst=0.0
    def connect(self):
        us=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        us.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try: us.bind(("",54298))
        except OSError: us.bind(("",0))
        us.settimeout(0.4)
        # WAKE: broadcast discovery "600TRANSPIC0430<TOKEN>" -> projectors answer 602 and
        # dormant ones activate so they will then answer the 500->501 association.
        bcast=".".join(self.ip.split(".")[:3]+["255"])
        disc=b"600TRANSPIC0430"+TOKEN.encode()
        for _ in range(8):
            for tgt in (bcast, self.ip):
                try: us.sendto(disc,(tgt,10000))
                except OSError: pass
            try:
                d,_=us.recvfrom(256)      # drain 602 replies
            except socket.timeout: pass
            time.sleep(0.1)
        threading.Thread(target=self._assoc_loop,args=(us,),daemon=True).start()
        # receive 501 -> derive m_CommKey
        t0=time.time(); blob=None
        while time.time()-t0<8 and blob is None:
            try:
                data,_=us.recvfrom(64)
                if data[:3]==b"501" and len(data)>=25: blob=data[9:25]
            except socket.timeout: pass
        if blob is None: raise RuntimeError("no 501 response (association failed)")
        self.commkey=bswap32(AES.new(self.pkey,AES.MODE_ECB).decrypt(blob)); self._associated=True
        print("501 blob:",blob.hex(),"-> m_CommKey:",self.commkey.hex())
        self._spawn_beacon()   # steady beacon runs OUT of process: encode load can't starve its cadence
        # TCP 12000 handshake (plaintext)
        self.ctl=socket.socket(); self.ctl.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1); self.ctl.settimeout(8); self.ctl.connect((self.ip,12000))
        self.ctl.sendall(f"INIT 07 RT 1024 768 {TOKEN} \r\nSTAT RES \r\nTYPE TCP none none\r\n".encode())
        time.sleep(1); self.ctl.setblocking(False); resp=b""
        for _ in range(15):
            try: resp+=self.ctl.recv(256)
            except BlockingIOError: time.sleep(0.2)
        self.ctl.setblocking(True)
        port=None
        for line in resp.decode("ascii","replace").split("\r\n"):
            if line.startswith("120 OK"): port=int(line.split()[-1])
            elif line.startswith("112 "):          # STAT RES -> the projector's real resolution
                try:
                    pw,ph=int(line.split()[1]),int(line.split()[2])
                    if pw>0 and ph>0:
                        self.panel_w,self.panel_h=pw,ph
                        print("[%s] reports %dx%d" % (self.ip,pw,ph))
                except (IndexError,ValueError): pass
        if not port: raise RuntimeError(f"no data port; reply={resp!r}")
        self.vs=socket.socket(); self.vs.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        if VIDEO_SNDBUF>0:
            try: self.vs.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,VIDEO_SNDBUF)  # BEFORE connect
            except OSError: pass
        self.vs.settimeout(15); self.vs.connect((self.ip,port))
        self.u12004=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        if CTL_LEGACY: self.u12004.sendto(bytes.fromhex("0540000000000000"),(self.ip,12004))
        self.vs.settimeout(SEND_TIMEOUT)   # generous on purpose: abandoning a merely-SLOW send
                                          # mid-frame is what hangs the module (v1.0 defect)
        self._last_ctl=time.time()
        threading.Thread(target=self._ka_loop, daemon=True).start()   # NOOP keepalive so idle never wedges
        self.port=port; print("data port:",port); return port
    def _ka_loop(self):
        # WM sends NOOP on the control channel ~every 4s. Without it an idle (static-screen, or
        # capture-denied/blank) session times out and the network module WEDGES. Keep it alive.
        while not self._stop:
            time.sleep(1.0)
            if self._stop: break
            if time.time()-self._last_ctl > 4.0:
                try:
                    with self._ctl_lock:
                        self.ctl.sendall(b"NOOP \r\n"); self._last_ctl=time.time()
                except Exception: break

    def encrypt_payload(self, payload):
        n=(len(payload)//16)*16
        return AES.new(self.commkey,AES.MODE_ECB).encrypt(payload[:n])+payload[n:]
    def strip(self, payload, jpegW, jpegH, yoff, codec=3, last=False):
        h=bytearray(24)
        h[0]=0x07; h[1]=0x01; h[2]=0x80 if last else 0x00; h[4]=codec
        if codec==3: h[5]=0x10   # codec-3 mode field is 0x0310 (observed)
        struct.pack_into(">H",h,6,0x0014)
        struct.pack_into(">I",h,8,len(payload))
        struct.pack_into(">H",h,12,jpegW); struct.pack_into(">H",h,14,jpegH)
        struct.pack_into(">H",h,18,yoff)
        struct.pack_into(">H",h,20,self.panel_w); struct.pack_into(">H",h,22,self.panel_h)
        return bytes(h)+self.encrypt_payload(payload)
    def send_scans(self, scans):
        """Encrypt (per-projector m_CommKey) + send pre-encoded scans (coalesced single write)."""
        if STRIP_DELAY:
            for scan,yoff,last,sh in scans:
                self.vs.sendall(self.strip(scan,self.panel_w,sh,yoff,codec=3,last=last)); time.sleep(STRIP_DELAY)
        else:
            buf=b"".join(self.strip(scan,self.panel_w,sh,yoff,codec=3,last=last) for scan,yoff,last,sh in scans)
            self.vs.sendall(buf)
        self._legacy_frame_tail()
    def send_strips(self, scans, idxs):
        """Send ONLY the changed bands (strip-level delta); lastsend on the final one."""
        if not idxs: return
        li=idxs[-1]
        buf=b"".join(self.strip(scans[i][0],self.panel_w,scans[i][3],scans[i][1],codec=3,last=(i==li)) for i in idxs)
        self.vs.sendall(buf)
        self._legacy_frame_tail()
    def _legacy_frame_tail(self):
        """Old per-frame 12004 pulse + NOOP. No vendor app does this; kept for A/B only."""
        if not CTL_LEGACY: return
        self.u12004.sendto(bytes.fromhex("0540000000000000"),(self.ip,12004))
        with self._ctl_lock: self.ctl.sendall(b"NOOP \r\n"); self._last_ctl=time.time()
    def send_jpeg_frame(self, img):
        self.send_scans(build_scans(img))
    def send_raw_strips(self, strips):     # replay already-encrypted strips (display test)
        for s in strips: self.vs.sendall(s); time.sleep(0.008)
        self._legacy_frame_tail()
    def _spawn_beacon(self):
        """Launch the steady beacon as a separate process (immune to the encoder's CPU/GIL load)."""
        if BEACON_PERIOD <= 0: return False
        import subprocess
        args=[self.ip, self.commkey.hex(), TOKEN, BEACON_USER, "%.3f" % BEACON_PERIOD]
        try:
            if getattr(sys,"frozen",False):
                cmd=[sys.executable,"--beacon-hold"]+args
            else:
                cmd=[sys.executable, os.path.abspath(__file__), "beacon-hold"]+args
            self._beacon_proc=subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("[%s] beacon: subprocess pid %d @ %.2fs (starvation-proof)"
                  % (self.ip, self._beacon_proc.pid, BEACON_PERIOD), flush=True)
            return True
        except Exception as e:
            print("[%s] beacon subprocess spawn failed (%s); in-process fallback" % (self.ip, e), flush=True)
            self._beacon_proc=None; return False

    def close(self):
        """Graceful shutdown: blank (black frame) -> video FIN -> QUIT/182 OK -> control FIN."""
        if getattr(self, "_closed", False): return   # idempotent: stop() and the worker may both call
        self._closed=True
        self._stop=True                      # stops association loop AND the caller's frame loop
        bp=getattr(self,"_beacon_proc",None)
        if bp is not None:
            try: bp.terminate()
            except Exception: pass
            self._beacon_proc=None
        time.sleep(0.15)                      # let the assoc loop stop sending
        # 0) BLANK the display so the projector doesn't freeze on the last mirrored frame
        try:
            from PIL import Image
            self.send_jpeg_frame(Image.new("RGB",(self.panel_w,self.panel_h),(0,0,0)))
            time.sleep(0.2)
        except Exception: pass
        # 1) close the video socket first (graceful FIN)
        try: self.vs.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        try: self.vs.close()
        except Exception: pass
        # 2) QUIT on control, wait for "182 OK"
        try:
            with self._ctl_lock: self.ctl.sendall(b"QUIT \r\n")
            self.ctl.settimeout(3)
            try: r=self.ctl.recv(64)
            except Exception: r=b""
            print("QUIT ->", r.decode("ascii","replace").strip() or "(no reply)")
            # 3) close control socket (graceful FIN)
            try: self.ctl.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            self.ctl.close()
        except Exception: pass

_MSS_TL=threading.local()   # mss instances are thread-bound on macOS; give each thread its own
def _mss():
    m=getattr(_MSS_TL,"inst",None)
    if m is None:
        import mss; m=mss.mss(); _MSS_TL.inst=m
    return m
def cg_display_for_mss_monitor(mon):
    """Map an mss monitor index (what the picker uses) to a CGDirectDisplayID (what CGDisplayStream
    needs), by matching display bounds. Returns None if no match."""
    try:
        import mss as _m
        _M = getattr(_m, "MSS", None) or _m.mss
        with _M() as m:
            if not (0 <= mon < len(m.monitors)): return None
            mo = m.monitors[mon]
        from Quartz import CGGetActiveDisplayList, CGDisplayBounds
        e, ids, c = CGGetActiveDisplayList(16, None, None)
        for d in list(ids)[:c]:
            b = CGDisplayBounds(d)
            if (int(b.origin.x) == mo["left"] and int(b.origin.y) == mo["top"]
                    and int(b.size.width) == mo["width"] and int(b.size.height) == mo["height"]):
                return int(d)
    except Exception:
        pass
    return None

def list_monitors():
    """Return [(index,label,w,h)] for the picker. 1=primary, 2+=extended/virtual displays.
    Uses a FRESH mss instance every call: mss enumerates displays once per instance, so the cached
    one never sees a virtual display added after startup (that made the picker miss it)."""
    try:
        import mss as _m
        _M = getattr(_m, "MSS", None) or _m.mss     # MSS class on new mss; mss() factory on old
        with _M() as m:
            out=[]
            for i,mo in enumerate(m.monitors):
                if i==0: continue   # skip the combined virtual bounding box
                out.append((i, ("Primary" if i==1 else "Display %d"%i), mo["width"], mo["height"]))
            return out
    except Exception: return [(1,"Primary",0,0)]
def grab_screen(shot_path=None, mon=1):
    """Fast screen grab of display <mon> via mss (cached); falls back to screencapture (display <mon>)."""
    from PIL import Image
    try:
        s=_mss(); mons=s.monitors
        m=mons[mon] if 0<=mon<len(mons) else mons[1]
        raw=s.grab(m)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception:
        import subprocess
        p=shot_path or "/tmp/pm_live.jpg"
        try:
            subprocess.run(["screencapture","-x","-t","jpg","-D",str(mon),p],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
            return Image.open(p)
        except Exception:
            return None

_POOL=None
def _safe_send(x, scans):
    try: x.send_scans(scans)
    except Exception as e: print(f"[{x.ip}] send error: {e}")
def _safe_send_strips(x, scans, idxs):
    try: x.send_strips(scans, idxs)
    except Exception as e: print(f"[{x.ip}] send error: {e}")

def _connect_with_retry(ip, tries=6):
    if not power_on_network(ip):
        print(f"[{ip}] projector did not reach the NETWORK input; connecting anyway")
    s=Sender(ip)
    for a in range(tries):
        try: s.connect(); return s
        except Exception as e:
            print(f"[{ip}] connect attempt {a+1} failed: {e}; retry in 8s (slot may be busy)")
            try: s.close()
            except Exception: pass
            s=Sender(ip); time.sleep(8)
    print(f"[{ip}] could NOT connect (slot wedged? reconnect WM once or wait)"); return None

def main():
    if len(sys.argv)>1 and sys.argv[1]=="beacon-hold":
        beacon_hold(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], float(sys.argv[6])); return
    import signal, tempfile
    a1 = sys.argv[1] if len(sys.argv)>1 else "dual"
    if a1=="dual":
        ips=list(PKEYS.keys()); mode="live"; argoff=2
    else:
        ips=[a1]; mode=(sys.argv[2] if len(sys.argv)>2 else "live"); argoff=3
    senders=[x for x in (_connect_with_retry(ip) for ip in ips) if x]
    if not senders: raise SystemExit("no projectors connected")
    print("connected:", [x.ip for x in senders])
    global _POOL
    from concurrent.futures import ThreadPoolExecutor
    _POOL=ThreadPoolExecutor(max_workers=max(1,len(senders)))

    # Stop signals -> graceful close via finally. Stop with Ctrl-C or `kill <pid>` (NEVER kill -9).
    def _sig(sig, frm): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig)

    try:
        if mode in ("live","dual"):
            fps  = float(sys.argv[argoff])   if len(sys.argv)>argoff   else 3.0
            secs = float(sys.argv[argoff+1]) if len(sys.argv)>argoff+1 else 0
            shot = tempfile.gettempdir()+"/pm_live.jpg"
            print(f"LIVE mirror -> {[x.ip for x in senders]} @ ~{fps} fps" +
                  (f" for {secs}s" if secs else " (Ctrl-C or kill to stop cleanly)"))
            # LOW-LATENCY: grab continuously; only SEND when the screen actually changed, so nothing
            # stale queues and an edit reaches the wall in ~one frame time. One frame in flight (sendall
            # blocks until the projector accepts) => no buffer bloat. fps floats with motion.
            import hashlib as _hl
            last=None; prev=None; frames=0; sent=0; t0=time.time(); target=(1.0/fps) if fps>0 else 0
            DELTA = os.environ.get("DELTA","1")!="0"   # strip-level delta (send only changed bands)
            while secs==0 or time.time()-t0<secs:
                img=grab_screen(shot)
                if img is None: time.sleep(0.02); continue
                h=zlib.crc32(img.tobytes())     # cheap change detection
                if h==last:
                    time.sleep(0.01); continue      # unchanged -> send nothing (projector holds frame)
                last=h; fs=time.time()
                scans=build_scans(img)
                if DELTA and prev is not None:
                    idxs=[i for i in range(len(scans)) if scans[i][0]!=prev[i][0]]
                else:
                    idxs=list(range(len(scans)))
                prev=scans
                if not idxs: time.sleep(0.005); continue
                if len(senders)>1 and _POOL:
                    list(_POOL.map(lambda x: _safe_send_strips(x,scans,idxs), senders))
                else:
                    for x in senders: _safe_send_strips(x,scans,idxs)
                sent+=1
                if sent%20==0: print(f"  {sent} sent, {sent/(time.time()-t0):.1f} fps, last delta={len(idxs)}/8 strips")
                if target: time.sleep(max(0.0, target-(time.time()-fs)))
        elif mode=="jpeg":
            from PIL import Image
            img=Image.open(sys.argv[argoff]) if len(sys.argv)>argoff else Image.new("RGB",(1024,768),(20,60,230))
            t0=time.time()
            while time.time()-t0<12:
                for x in senders: x.send_jpeg_frame(img)
                time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n[stopping cleanly]")
    finally:
        for x in senders:
            try: x.close()
            except Exception: pass
        print("done (clean stop)")

if __name__=="__main__": main()
