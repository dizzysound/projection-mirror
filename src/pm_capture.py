#!/usr/bin/env python3
"""
pm_capture.py - low-latency screen capture via CGDisplayStream (V2e).

Replaces the mss poll: the OS PUSHES frames (event-driven, so no poll-interval latency) as GPU
IOSurfaces already scaled to the panel width (the GPU does the downscale, removing the ~18-28 ms/frame
CPU fit_panel resize). Steady-state IOSurface->PIL readback is ~3.7 ms via CIContext.render(toBitmap).

CGDisplayStream is deprecated on macOS 14+ (still runs) - ScreenCaptureKit is the forward path; this
is the backend that works on the test Mac (12.7) and today's M1. Needs Screen Recording for the process.
Falls back to mss if unavailable (available() / start() failure).
"""
import threading, ctypes, time, os
try:
    import objc, Quartz
    from Quartz import (CGDisplayPixelsWide, CGDisplayPixelsHigh, CGColorSpaceCreateDeviceRGB,
                        CGRectMake, kCIFormatRGBA8)
    from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
    _HAVE = hasattr(Quartz, "CGDisplayStreamCreateWithDispatchQueue")
except Exception:
    _HAVE = False
from PIL import Image
import platform, zlib

try:
    import ScreenCaptureKit as _sck
    from CoreMedia import CMSampleBufferGetImageBuffer, CMSampleBufferIsValid, CMTimeMake
    _HAVE_SCK = hasattr(_sck, "SCStream")
except Exception:
    _HAVE_SCK = False

def _macos_major():
    try: return int(platform.mac_ver()[0].split(".")[0])
    except Exception: return 0

def available_sck():
    return _HAVE_SCK

BGRA = 0x42475241
FLIP = os.environ.get("STREAM_FLIP", "auto")   # "auto" | "1" | "0" - vertical flip of stream frames

def available():
    return _HAVE


class DisplayStreamSource:
    """Push-capture one display, delivering panel-sized (letterboxed) RGB frames. get() returns
    (seq, PIL.Image); seq increments on each new frame so the producer sends only on change."""

    def __init__(self, display_id, panel_w=1024, panel_h=768, letterbox=True):
        self.display_id = int(display_id)
        self.panel_w, self.panel_h, self.letterbox = panel_w, panel_h, letterbox
        self._lock = threading.Lock()
        self._img = None
        self._seq = 0
        self._stop = False
        self._thread = None
        self._flip = None if FLIP == "auto" else (FLIP == "1")
        self._ctx = None; self._cs = None
        # output (GPU-scaled) size: letterbox = fit source aspect into panel; else = panel
        sw, sh = CGDisplayPixelsWide(self.display_id), CGDisplayPixelsHigh(self.display_id)
        if letterbox and sw and sh:
            sc = min(panel_w / sw, panel_h / sh)
            self.out_w, self.out_h = max(2, int(sw * sc) // 2 * 2), max(2, int(sh * sc) // 2 * 2)
        else:
            self.out_w, self.out_h = panel_w, panel_h

    def start(self, timeout=4.0):
        if not _HAVE:
            return False
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        t0 = time.time()
        while time.time() - t0 < timeout and self._seq == 0 and not self._stop:
            time.sleep(0.02)
        return self._seq > 0

    def _to_pil(self, surface):
        ci = Quartz.CIImage.imageWithIOSurface_(surface)
        w, h, bpr = self.out_w, self.out_h, self.out_w * 4
        # PER-CALL buffer. A shared self._buf tore frames when two handlers overlapped: each
        # rendered a different screen state into the same bytes, and both copied out the mix.
        # SCKSource already allocates per sample; this keeps the two backends honest.
        buf = bytearray(bpr * h)
        self._ctx.render_toBitmap_rowBytes_bounds_format_colorSpace_(
            ci, buf, bpr, CGRectMake(0, 0, w, h), kCIFormatRGBA8, self._cs)
        im = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "RGBA", bpr, 1).convert("RGB")
        if self._flip:
            im = im.transpose(Image.FLIP_TOP_BOTTOM)
        if self.letterbox and (w != self.panel_w or h != self.panel_h):
            canvas = Image.new("RGB", (self.panel_w, self.panel_h), (0, 0, 0))
            canvas.paste(im, ((self.panel_w - w) // 2, (self.panel_h - h) // 2))
            return canvas
        return im

    def _make_queue(self):
        """SERIAL queue for the CGDisplayStream handler.

        The global queue is CONCURRENT, so handler invocations overlap on different threads. That
        both tore the bitmap (shared destination buffer) and let a late frame publish over a newer
        one. dispatch_queue_create(label, NULL) is serial, so frames are handled in order, one at a
        time - which is also what Apple's own CGDisplayStream sample does."""
        libc = ctypes.CDLL(None)
        libc.dispatch_queue_create.restype = ctypes.c_void_p
        libc.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        q = libc.dispatch_queue_create(b"org.projectionmirror.capture", None)
        return objc.objc_object(c_void_p=q)

    def _run(self):
        self._ctx = Quartz.CIContext.contextWithOptions_(None)
        self._cs = CGColorSpaceCreateDeviceRGB()
        queue = self._make_queue()

        def handler(status, t, surface, upd):
            if status != 0 or surface is None:
                return
            try:
                im = self._to_pil(surface)
                with self._lock:
                    self._img = im; self._seq += 1
            except Exception:
                pass

        stream = Quartz.CGDisplayStreamCreateWithDispatchQueue(
            self.display_id, self.out_w, self.out_h, BGRA, None, queue, handler)
        if stream is None:
            self._stop = True; return
        Quartz.CGDisplayStreamStart(stream)
        while not self._stop:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)
        Quartz.CGDisplayStreamStop(stream)

    def get(self):
        with self._lock:
            return self._seq, self._img

    def stop(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=2)


class SCKSource:
    """ScreenCaptureKit backend (the forward path; CGDisplayStream is deprecated on macOS 14+).
    Same interface as DisplayStreamSource: get() -> (seq, panel-sized RGB PIL image). SCK pushes at
    a fixed rate, so we crc32-dedup to keep send-on-change semantics."""

    def __init__(self, display_id, panel_w=1024, panel_h=768, letterbox=True, max_fps=30):
        self.display_id = int(display_id)
        self.panel_w, self.panel_h, self.letterbox = panel_w, panel_h, letterbox
        self.max_fps = max_fps
        self._lock = threading.Lock(); self._img = None; self._seq = 0; self._last_crc = None
        self._stream = None; self._out = None; self._ctx = None; self._cs = None
        sw = _sck and 0  # noqa
        # output (SCK-scaled) size preserving aspect when letterboxing
        from Quartz import CGDisplayPixelsWide, CGDisplayPixelsHigh
        dw, dh = CGDisplayPixelsWide(self.display_id), CGDisplayPixelsHigh(self.display_id)
        if letterbox and dw and dh:
            sc = min(panel_w / dw, panel_h / dh)
            self.out_w, self.out_h = max(2, int(dw*sc)//2*2), max(2, int(dh*sc)//2*2)
        else:
            self.out_w, self.out_h = panel_w, panel_h

    def start(self, timeout=6.0):
        if not _HAVE_SCK: return False
        import ctypes
        from Quartz import CGColorSpaceCreateDeviceRGB
        self._ctx = Quartz.CIContext.contextWithOptions_(None); self._cs = CGColorSpaceCreateDeviceRGB()
        box = {"done": threading.Event()}
        def got(c, e): box["c"] = c; box["e"] = e; box["done"].set()
        _sck.SCShareableContent.getShareableContentWithCompletionHandler_(got)
        if not box["done"].wait(timeout): return False
        content = box.get("c")
        if content is None: return False
        disp = None
        for d in content.displays():
            if d.displayID() == self.display_id: disp = d; break
        if disp is None: return False
        filt = _sck.SCContentFilter.alloc().initWithDisplay_excludingWindows_(disp, [])
        cfg = _sck.SCStreamConfiguration.alloc().init()
        cfg.setWidth_(self.out_w); cfg.setHeight_(self.out_h); cfg.setPixelFormat_(BGRA)
        try: cfg.setMinimumFrameInterval_(CMTimeMake(1, max(1, self.max_fps)))
        except Exception: pass
        try: cfg.setShowsCursor_(True)
        except Exception: pass
        outer = self
        NSObject = objc.lookUpClass("NSObject")
        class _Out(NSObject):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
                outer._on_sample(sbuf, otype)
        self._out = _Out.alloc().init()
        self._stream = _sck.SCStream.alloc().initWithFilter_configuration_delegate_(filt, cfg, None)
        libc = ctypes.CDLL(None); libc.dispatch_get_global_queue.restype = ctypes.c_void_p
        libc.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
        queue = objc.objc_object(c_void_p=libc.dispatch_get_global_queue(0, 0))
        ok, err = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(self._out, 0, queue, None)
        if not ok: return False
        sb = {"done": threading.Event()}
        def started(e): sb["e"] = e; sb["done"].set()
        self._stream.startCaptureWithCompletionHandler_(started)
        sb["done"].wait(timeout)
        t0 = time.time()
        while time.time() - t0 < timeout and self._seq == 0:
            time.sleep(0.02)
        return self._seq > 0

    def _on_sample(self, sbuf, otype):
        try:
            if otype != 0 or not CMSampleBufferIsValid(sbuf): return
            pb = CMSampleBufferGetImageBuffer(sbuf)
            if pb is None: return
            from Quartz import CGRectMake, kCIFormatRGBA8
            ci = Quartz.CIImage.imageWithCVImageBuffer_(pb)
            w, h, bpr = self.out_w, self.out_h, self.out_w * 4
            buf = bytearray(bpr * h)
            self._ctx.render_toBitmap_rowBytes_bounds_format_colorSpace_(
                ci, buf, bpr, CGRectMake(0, 0, w, h), kCIFormatRGBA8, self._cs)
            crc = zlib.crc32(buf)
            if crc == self._last_crc: return            # dedup: only advance on real change
            self._last_crc = crc
            im = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "RGBA", bpr, 1).convert("RGB")
            if self.letterbox and (w != self.panel_w or h != self.panel_h):
                canvas = Image.new("RGB", (self.panel_w, self.panel_h), (0, 0, 0))
                canvas.paste(im, ((self.panel_w - w) // 2, (self.panel_h - h) // 2)); im = canvas
            with self._lock:
                self._img = im; self._seq += 1
        except Exception:
            pass

    def get(self):
        with self._lock:
            return self._seq, self._img

    def stop(self):
        st = self._stream
        self._stream = None
        if st is not None:
            ev = threading.Event()
            def done(e): ev.set()
            try:
                st.stopCaptureWithCompletionHandler_(done); ev.wait(2.0)
            except Exception:
                pass


def make_source(display_id, panel_w=1024, panel_h=768, letterbox=True, backend="auto"):
    """Pick a capture backend. 'auto' = ScreenCaptureKit on macOS >= 14 (CGDisplayStream is
    deprecated there), else CGDisplayStream; each falls back to the next, then to None (mss poll).
    Override with STREAM_BACKEND=sck|cgds|off."""
    import os as _os
    backend = _os.environ.get("STREAM_BACKEND", backend)
    order = []
    if backend == "sck": order = ["sck"]
    elif backend == "cgds": order = ["cgds"]
    elif backend == "off": order = []
    else:  # auto
        order = (["sck", "cgds"] if (_macos_major() >= 14 and _HAVE_SCK) else ["cgds", "sck"])
    for b in order:
        try:
            if b == "sck" and _HAVE_SCK:
                s = SCKSource(display_id, panel_w, panel_h, letterbox)
            elif b == "cgds" and _HAVE:
                s = DisplayStreamSource(display_id, panel_w, panel_h, letterbox)
            else:
                continue
            if s.start(timeout=5):
                return b, s
            s.stop()
        except Exception:
            pass
    return None, None


if __name__ == "__main__":   # orientation self-test: compare stream frame vs mss (known top-left)
    import sys
    from Quartz import CGMainDisplayID
    if not available():
        print("CGDisplayStream not available"); sys.exit(1)
    src = DisplayStreamSource(CGMainDisplayID(), 1024, 768, letterbox=True)
    src._flip = False
    if not src.start(timeout=6):
        print("no frame (static screen? play a video / move the mouse)"); sys.exit(0)
    seq, stream_img = src.get(); src.stop()
    try:
        import mss
        M = getattr(mss, "MSS", None) or mss.mss
        with M() as m:
            raw = m.grab(m.monitors[1])
            mss_full = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception as e:
        print("mss compare unavailable (%s); saved /tmp/v2e_cap.jpg for manual check" % e)
        stream_img.save("/tmp/v2e_cap.jpg"); sys.exit(0)
    # fit mss to the stream's letterboxed panel for comparison
    sw, sh = mss_full.size; sc = min(1024/sw, 768/sh)
    ow, oh = int(sw*sc), int(sh*sc)
    mfit = Image.new("RGB",(1024,768)); r=mss_full.resize((ow,oh)); mfit.paste(r,((1024-ow)//2,(768-oh)//2))
    def rowmeans(im): 
        g=im.convert("L").resize((16,64)); px=list(g.getdata()); return [sum(px[i*16:(i+1)*16]) for i in range(64)]
    a=rowmeans(stream_img); b=rowmeans(mfit); bflip=rowmeans(mfit.transpose(Image.FLIP_TOP_BOTTOM))
    d_same=sum(abs(x-y) for x,y in zip(a,b)); d_flip=sum(abs(x-y) for x,y in zip(a,bflip))
    print("orientation: stream vs mss same=%d flipped=%d -> %s"
          % (d_same, d_flip, "NO FLIP needed" if d_same<=d_flip else "FLIP needed (set STREAM_FLIP=1)"))
