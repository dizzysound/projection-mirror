# V2e prototype: CGDisplayStream (push) capture. Measures event-driven frame delivery + proves
# pixel access, vs the current mss poll. Captures the MAIN display for a few seconds.
import objc, ctypes, time, sys
import Quartz
from Quartz import (CGMainDisplayID, CGDisplayPixelsWide, CGDisplayPixelsHigh)
from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode

disp = CGMainDisplayID()
W, H = CGDisplayPixelsWide(disp), CGDisplayPixelsHigh(disp)
OUT_W, OUT_H = 1024, 768        # ask the stream to scale to panel size (GPU-side)
BGRA = 0x42475241              # 'BGRA'

libc = ctypes.CDLL(None)
libc.dispatch_get_global_queue.restype = ctypes.c_void_p
libc.dispatch_get_global_queue.argtypes = [ctypes.c_long, ctypes.c_ulong]
queue = objc.objc_object(c_void_p=libc.dispatch_get_global_queue(0, 0))

state = {"n": 0, "first": None, "last": None, "px_ok": None, "px_err": None}
FRAME_COMPLETE = 0  # kCGDisplayStreamFrameStatusFrameComplete

def handler(status, displayTime, surface, updateRef):
    if status != FRAME_COMPLETE or surface is None:
        return
    now = time.time()
    if state["first"] is None: state["first"] = now
    state["last"] = now; state["n"] += 1
    if state["px_ok"] is None:      # prove pixel access ONCE via Core Image
        try:
            ci = Quartz.CIImage.imageWithIOSurface_(surface)
            ext = ci.extent()
            state["px_ok"] = (int(ext.size.width), int(ext.size.height))
        except Exception as e:
            state["px_err"] = repr(e); state["px_ok"] = False

props = None
stream = Quartz.CGDisplayStreamCreateWithDispatchQueue(disp, OUT_W, OUT_H, BGRA, props, queue, handler)
if stream is None:
    print("CGDisplayStreamCreateWithDispatchQueue returned None"); sys.exit(1)
err = Quartz.CGDisplayStreamStart(stream)
print("stream started rc=%s  source=%dx%d -> %dx%d" % (err, W, H, OUT_W, OUT_H))
SECS = 8
for _ in range(int(SECS/0.1)):
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, False)
Quartz.CGDisplayStreamStop(stream)
el = (state["last"] - state["first"]) if state["first"] and state["last"] else 0
print("frames=%d over %.1fs  => %.1f fps (event-driven: 0 on a static screen is correct)"
      % (state["n"], SECS, (state["n"]/SECS)))
print("pixel access via CIImage(IOSurface):", state["px_ok"], state["px_err"] or "")
