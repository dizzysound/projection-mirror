#!/usr/bin/env python3
"""
pm_vdisplay.py - a native 1024x768 (or panel-sized) VIRTUAL display via macOS's private
CGVirtualDisplay API. Gives a single-screen laptop a dedicated presentation surface the projector
mirrors, and removes the per-frame downscale of a large physical display (the big encoding-throughput
win: build_scans ~7.6 ms native vs ~28.3 ms for a 2560 source on the M1).

WORKING RECIPE (proven on the test Mac, macOS 12.7, 2026-09-09):
  1. A persistent dispatch queue (the global concurrent queue - a singleton, so no retain lifetime
     bug from wrapping a ctypes pointer).
  2. setDispatchQueue_ on the descriptor.
  3. **Pump a CFRunLoop after applySettings_** - CGVirtualDisplay registers ASYNCHRONOUSLY on that
     queue; with no run loop the setup half-completes and DROPS the main display. Pumping the run
     loop is what makes it additive and lets the 1024x768 mode take. Keep pumping to hold it alive.
  4. **Display Mirroring must be OFF** (Displays > turn off mirroring) for it to be a usable EXTENDED
     display rather than cloning the main one.
Fully reversible: releasing the CGVirtualDisplay object (or exiting) removes it and restores config.
"""
import threading, objc, ctypes
from Quartz import CGSizeMake, CGGetActiveDisplayList, CGMainDisplayID
from CoreFoundation import (CFRunLoopRunInMode, kCFRunLoopDefaultMode, CFRunLoopGetCurrent)

def _load_classes():
    CGMainDisplayID()   # force CoreGraphics/SkyLight load so the private classes register
    return (objc.lookUpClass('CGVirtualDisplayDescriptor'),
            objc.lookUpClass('CGVirtualDisplay'),
            objc.lookUpClass('CGVirtualDisplayMode'),
            objc.lookUpClass('CGVirtualDisplaySettings'))

def available():
    try: _load_classes(); return True
    except Exception: return False


class VirtualDisplay:
    """Create + hold a virtual display for the life of this object. start() blocks until the display
    is up (or times out); stop() tears it down. Thread-safe start/stop; the display is held alive by
    a CFRunLoop on a dedicated thread."""

    def __init__(self, width=1024, height=768, name="F300U Virtual",
                 product_id=0x1234, vendor_id=0x3456, size_mm=(270.0, 200.0)):
        self.width, self.height, self.name = width, height, name
        self.product_id, self.vendor_id, self.size_mm = product_id, vendor_id, size_mm
        self.display_id = None
        self._disp = None
        self._thread = None
        self._runloop = None
        self._up = threading.Event()
        self._stop = False

    def start(self, timeout=6.0):
        if self._thread and self._thread.is_alive():
            return self.display_id
        self._up.clear()
        self._thread = threading.Thread(target=self._run, name="vdisplay", daemon=True)
        self._thread.start()
        self._up.wait(timeout)
        return self.display_id

    def _run(self):
        Desc, VDisp, Mode, Setts = _load_classes()
        libc = ctypes.CDLL(None)
        libc.dispatch_get_global_queue.restype = ctypes.c_void_p
        libc.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
        queue = objc.objc_object(c_void_p=libc.dispatch_get_global_queue(0, 0))  # singleton

        d = Desc.alloc().init()
        d.setName_(self.name)
        d.setMaxPixelsWide_(self.width); d.setMaxPixelsHigh_(self.height)
        d.setSizeInMillimeters_(CGSizeMake(*self.size_mm))
        d.setProductID_(self.product_id); d.setVendorID_(self.vendor_id)
        d.setDispatchQueue_(queue)
        self._disp = VDisp.alloc().initWithDescriptor_(d)
        mode = Mode.alloc().initWithWidth_height_refreshRate_(self.width, self.height, 60.0)
        st = Setts.alloc().init(); st.setHiDPI_(0); st.setModes_([mode])
        self._disp.applySettings_(st)
        self.display_id = int(self._disp.displayID())
        self._runloop = CFRunLoopGetCurrent()
        # pump until the 1024x768 mode takes (marks "up"); the setup is async on the queue
        from Quartz import CGDisplayPixelsWide, CGDisplayPixelsHigh
        for _ in range(40):
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)
            if CGDisplayPixelsWide(self.display_id) == self.width and \
               CGDisplayPixelsHigh(self.display_id) == self.height:
                break
        self._up.set()
        # hold the display alive by keeping the run loop serviced
        while not self._stop:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)
        # teardown: release, then PUMP so the async removal completes (else it lingers)
        self._disp = None
        for _ in range(12):
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)

    def stop(self):
        self._stop = True
        if self._thread: self._thread.join(timeout=6)
        self.display_id = None

    def __enter__(self): self.start(); return self
    def __exit__(self, *a): self.stop()


def hold(width=1024, height=768):
    """Create the virtual display and BLOCK forever (until the process is killed). This is how the
    app runs it: as a subprocess, so 'disable' = terminate the process, which removes the display
    reliably (in-process release lingers until the owning process exits)."""
    from Quartz import CGDisplayPixelsWide, CGDisplayPixelsHigh
    Desc, VDisp, Mode, Setts = _load_classes()
    libc = ctypes.CDLL(None)
    libc.dispatch_get_global_queue.restype = ctypes.c_void_p
    libc.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
    queue = objc.objc_object(c_void_p=libc.dispatch_get_global_queue(0, 0))
    d = Desc.alloc().init()
    d.setName_("F300U Virtual %dx%d" % (width, height))
    d.setMaxPixelsWide_(width); d.setMaxPixelsHigh_(height)
    d.setSizeInMillimeters_(CGSizeMake(270.0, 200.0))
    d.setProductID_(0x1234); d.setVendorID_(0x3456); d.setDispatchQueue_(queue)
    disp = VDisp.alloc().initWithDescriptor_(d)
    mode = Mode.alloc().initWithWidth_height_refreshRate_(width, height, 60.0)
    st = Setts.alloc().init(); st.setHiDPI_(0); st.setModes_([mode])
    disp.applySettings_(st)
    vid = int(disp.displayID())
    # pump until the mode takes; print READY once it is (or after a grace period) so a parent can sync
    ready = False
    for _ in range(40):
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, False)
        if CGDisplayPixelsWide(vid) == width and CGDisplayPixelsHigh(vid) == height:
            ready = True; break
    print("READY id=%d %dx%d%s" % (vid, CGDisplayPixelsWide(vid), CGDisplayPixelsHigh(vid),
                                   "" if ready else " (mode not applied - is Display Mirroring OFF?)"),
          flush=True)
    while True:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.0, False)


if __name__ == "__main__":
    import sys as _s
    if not available():
        print("CGVirtualDisplay not available on this macOS"); raise SystemExit(1)
    if len(_s.argv) > 1 and _s.argv[1] == "hold":
        w = int(_s.argv[2]) if len(_s.argv) > 2 else 1024
        h = int(_s.argv[3]) if len(_s.argv) > 3 else 768
        hold(w, h)                       # blocks until killed
    else:
        import time
        vd = VirtualDisplay(); vid = vd.start()
        from Quartz import CGDisplayPixelsWide, CGDisplayPixelsHigh
        print("virtual id=%s %dx%d" % (vid, CGDisplayPixelsWide(vid), CGDisplayPixelsHigh(vid)))
        time.sleep(5); vd.stop(); print("stopped")
