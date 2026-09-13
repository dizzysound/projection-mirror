#!/usr/bin/env python3
"""
pm_app.py - NATIVE macOS control panel for the Projection Mirror.
Real AppKit window (no browser). Reuses pm_mirror_v2.py as the engine.

Panel: registered-projector checkboxes, source-display picker, letterbox, Start/Pause/Stop,
HQ/Fast picture mode, Power/Input/Blank, WEB Control, status log.
Live parameter changes are frame-atomic in the engine, so sliders don't break the stream.
Stop hardware-blanks each projector via PJLink AV-mute, so the wall never freezes on the last frame.
"""
import threading, time, os, json, sys, logging, logging.handlers
import objc
import pm_mirror_v2 as fm
import pm_session as fs
import pm_registry as reg
import pm_vdisplay as vdisp
import pm_control as ctlsrv
from AppKit import (
    NSSegmentedControl,
    NSApplication, NSApp, NSObject, NSWindow, NSView, NSButton, NSSlider, NSTextField,
    NSPopUpButton, NSTextView, NSScrollView, NSColor, NSFont, NSTimer, NSMenu, NSMenuItem, NSBox,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
    NSBackingStoreBuffered, NSButtonTypeSwitch, NSApplicationActivationPolicyRegular,
    NSMakeRect, NSTextAlignmentRight, NSBezelStyleRounded, NSFontWeightSemibold, NSWorkspace,
)
from Foundation import NSMakePoint, NSURL, NSProcessInfo

# NSActivityOptions (numeric, to avoid relying on the bridged constants): keep macOS from App-Napping
# or coalescing/throttling our background streaming loops while a mirror is live. Without this the
# app naps after ~a minute of no interaction, the producer/sender poll loops are throttled, and the
# wall stalls until something wakes the app (the 100%-CPU spin we removed in 2.7 had masked this by
# keeping the app permanently "active").
_ACT_IDLE_DISPLAY_SLEEP_DISABLED = (1 << 40)
_ACT_USER_INITIATED              = 0x00FFFFFF          # includes IdleSystemSleepDisabled (bit 20)
_ACT_LATENCY_CRITICAL            = 0xFF00000000
NS_ACTIVITY_OPTS = (_ACT_USER_INITIATED | _ACT_LATENCY_CRITICAL | _ACT_IDLE_DISPLAY_SLEEP_DISABLED)

# Two picture modes. Both use full-JFIF strips (any quality decodes); picture *quality* controls
# live in the projector's own menu. HQ pins q100 (best quality, ~4 fps). Adaptive (default) runs
# the q50-100 byte governor - it trades quality for frame rate against a byte budget, floats to q100
# on static slides, and holds fps on video. The floor rationale is in the MODES dict comment below.
MODES = {
    # HQ pins q100 (best quality; the projector caps it near ~4 fps above the bandwidth knee).
    # Adaptive runs the q50-100 governor. Floor history: q15 -> q75 -> q50 (Chris, 2026-09-11)
    # for more fps headroom when a frame is busy - the governor still floats to q100 on static/light
    # content and only drops toward the floor under load. From the fitted send model
    # (send_ms = 0.519*kB + 32.4, a 1.88 MB/s module drain + 32ms latency): SLIDE q75 107kB 11.4fps,
    # q85 130kB 10.0fps, q100 319kB 5.1fps. The byte curve KNEES at ~q85, so quality above the knee
    # is nearly free; q50 sits below the knee - it spends real quality on the busiest frames to buy
    # fps, but only when the governor needs it (a light/static screen still gets q100).
    "hq":   dict(label="HQ (q100)", qmin=100, qmax=100, q=100, interval=0.0, governor=False),
    "fast": dict(label="Adaptive",  qmin=50,  qmax=100, q=85,  interval=0.0, governor=True),
}
ADAPTIVE_KBPS = float(os.environ.get("ADAPTIVE_KBPS", "700"))   # fixed Adaptive governor target (no slider)
SETTINGS_PATH = os.path.expanduser("~/Library/Application Support/ProjectionMirror/settings.json")
LOG_PATH = os.path.expanduser("~/Library/Logs/ProjectionMirror.log")

def _setup_logging():
    """Rotating debug log: 2MB x 6 files at ~/Library/Logs/ProjectionMirror.log*"""
    try: os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    except Exception: pass
    lg = logging.getLogger("f300u"); lg.setLevel(logging.DEBUG); lg.propagate = False
    if not lg.handlers:
        try:
            h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=5)
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s [%(threadName)s] %(message)s"))
            lg.addHandler(h)
        except Exception: pass
    return lg

LOG = _setup_logging()

class _StreamToLog:
    """The windowed bundle discards stdout, so capture engine print()/tracebacks into the log."""
    def __init__(self, level): self.level = level; self._buf = ""
    def write(self, txt):
        try:
            self._buf += txt
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip(): LOG.log(self.level, "engine| " + line.rstrip())
        except Exception: pass
    def flush(self):
        try:
            if self._buf.strip(): LOG.log(self.level, "engine| " + self._buf.strip())
            self._buf = ""
        except Exception: pass
    def isatty(self): return False

_CG = None
def _coregraphics():
    global _CG
    if _CG is None:
        try:
            import ctypes, ctypes.util
            p = (ctypes.util.find_library("CoreGraphics")
                 or "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg = ctypes.CDLL(p)
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
            _CG = cg
        except Exception:
            _CG = False
    return _CG or None

VERSION = "2.8"
BUNDLE_ID = "org.projectionmirror.app"

def perm_preflight():
    """Silent check. None = can't tell."""
    cg = _coregraphics()
    if cg is None: return None
    try: return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception: return None

def perm_request():
    """Shows the system prompt (only if macOS has no decision recorded yet)."""
    cg = _coregraphics()
    if cg is None: return None
    try: return bool(cg.CGRequestScreenCaptureAccess())
    except Exception: return None

def capture_capable():
    """REAL proof of Screen Recording. Without the grant macOS hides other apps' window TITLES,
    so this distinguishes a genuine capture from the wallpaper-only fallback (which still returns
    a correctly-sized image and fooled an earlier size-based self-test). None = can't tell."""
    try:
        from Quartz import (CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly,
                            kCGNullWindowID)
    except Exception:
        return None
    try:
        wins = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []
        me = os.getpid()
        named = [w for w in wins if w.get("kCGWindowName")
                 and w.get("kCGWindowOwnerPID") != me]
        return len(named) > 0
    except Exception:
        return None

def perm_reset():
    """Clear a STALE TCC entry. After a rebuild macOS often keeps the old row switched ON while
    the new binary is NOT actually granted - and because a decision exists, no prompt appears.
    Resetting makes the next request prompt fresh."""
    try:
        import subprocess
        subprocess.run(["tccutil", "reset", "ScreenCapture", BUNDLE_ID],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return True
    except Exception:
        return False

def _lbl(text, x, y, w, h, bold=False, size=12, right=False):
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    f.setStringValue_(text); f.setBezeled_(False); f.setDrawsBackground_(False)
    f.setEditable_(False); f.setSelectable_(False)
    f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    if right: f.setAlignment_(NSTextAlignmentRight)
    return f

class Delegate(NSObject):
    def init(self):
        self = objc.super(Delegate, self).init()
        if self is None: return None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()   # set == paused; see pause_()
        self.thread = None
        self.log_lines = []
        self._log_lock = threading.Lock()
        self.mirroring = False
        self.blanked = False
        self._fps_last = {}
        self.projectors = None    # registered list: [{"ip","name","on"}], see pm_registry
        self.cb_pj = []           # one checkbox per registered projector, same order
        self.mgr_win = None
        self._mgr_fields = []
        return self

    # ---------- logging ----------
    @objc.python_method
    def log(self, m):
        line = time.strftime("%H:%M:%S ") + str(m)
        with self._log_lock:
            self.log_lines.append(line)
            self.log_lines = self.log_lines[-200:]
        LOG.info(str(m))

    @objc.python_method
    def _on_main(self, fn):
        """AppKit is main-thread only. Background threads must marshal UI updates here,
        otherwise you get 'deleted thread with uncommitted CATransaction' and UI corruption.
        The block is wrapped: a Python exception inside a main-queue block becomes an UNCAUGHT
        ObjC exception and aborts the whole app (SIGABRT on quit, 2026-09-09 14:38:47 - the
        'Stopped' UI update ran while the app was terminating). Log it, never throw it; and
        drop UI work once termination has begun."""
        def safe():
            if getattr(self, "_terminating", False): return
            try: fn()
            except Exception: LOG.debug("UI update failed (ignored)", exc_info=True)
        try:
            from Foundation import NSOperationQueue
            NSOperationQueue.mainQueue().addOperationWithBlock_(safe)
        except Exception:
            safe()

    def refreshLog_(self, timer):
        try:
            self.logview.setString_("\n".join(self.log_lines))
            self.logview.scrollRangeToVisible_((len(self.logview.string()), 0))
            _sess = getattr(self, "mirror_session", None)
            if (self.mirroring and _sess is not None and not self.stop_event.is_set()
                    and not _sess.producer_alive()):
                self.log("capture stopped unexpectedly - tearing down the mirror")
                self.stop_event.set()             # unblocks _mirror -> sess.stop() -> UI resets to idle
            if self.mirroring and _sess is not None:
                now = time.time(); parts = []
                for ip, v in _sess.summary().items():
                    cnt = v.get("sent", 0)
                    pc, pt = self._fps_last.get(ip, (0, now)); dt = now - pt
                    fps = (cnt - pc) / dt if dt > 0 else 0.0
                    self._fps_last[ip] = (cnt, now)
                    tail = ip.split(".")[-1]
                    parts.append(".%s reconnecting" % tail if not v.get("alive", False)
                                 else ".%s %.1f fps" % (tail, fps))
                paused = self.pause_event.is_set()
                self.status.setStringValue_(("PAUSED   " if paused else "MIRRORING   ")
                                            + "   ".join(parts))
                self.status.setTextColor_(NSColor.systemOrangeColor() if paused
                                          else NSColor.systemGreenColor())
            else:
                self.status.setStringValue_("idle")
                self.status.setTextColor_(NSColor.secondaryLabelColor())
                self._fps_last = {}
        except Exception: pass

    # ---------- window ----------
    def applicationDidFinishLaunching_(self, note):
        # Permission FIRST: prompt now rather than after Start shows a blank wall.
        self.log("--- launched; debug log: %s" % LOG_PATH)
        self.log("control plane: %s | keepalive NOOP ~4s"
                 % ("LEGACY per-frame NOOP+12004 (wedges!)" if fm.CTL_LEGACY else "vendor-exact"))
        self._perm_granted = self._ensure_permission()
        self.settings = self._load_settings()
        self.projectors = reg.from_settings(self.settings)
        self._build_window()
        if self._perm_granted:
            self.win.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        else:
            # Do NOT show our window yet: it lands on top of the macOS permission prompt and
            # hides it. Give the prompt a clear field, then step in with guidance.
            LOG.info("holding main window back so the system prompt stays visible")
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                6.0, self, "showAfterPrompt:", None, False)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.7, self, "refreshLog:", None, True)
        if not self._perm_granted:      # watch for the grant, then relaunch automatically
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                2.0, self, "checkPermission:", None, True)
            # guidance comes from showAfterPrompt_ once the system prompt has had its chance
        self.log("Ready. Pick projectors + display, then Start.")

    @objc.python_method
    def _build_window(self):
        """Builds the whole panel from self.projectors. Re-run after the registered list changes
        (the projector rows are dynamic, so the layout below them moves)."""
        st = self.settings = self._load_settings()
        if self.projectors is None:          # first build of this launch
            self.projectors = reg.from_settings(st)
        pjs = self.projectors = reg.normalize(self.projectors)
        rows = max(1, (len(pjs) + 1) // 2)
        W, H = 520, 700 + 28 * (rows - 1)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setTitle_("Projection Mirror  v%s" % VERSION)
        win.center()
        old = getattr(self, "win", None)
        self.win = win
        v = win.contentView()

        def add(ctl): v.addSubview_(ctl); return ctl

        y = H - 40
        add(_lbl("Projection Mirror  v%s" % VERSION, 20, y, 360, 24, bold=True, size=17))
        add(self._button("Manage…", 390, y - 4, 110, "manageProjectors:"))
        y -= 34

        # projectors (dynamic - the registered list, not two baked IPs)
        add(_lbl("Projectors", 20, y, 200, 18, bold=True))
        y -= 26
        self.cb_pj = []
        for i, p in enumerate(pjs):
            x = 30 if i % 2 == 0 else 270
            cb = add(self._switch("%s  ·  %s" % (p["name"], p["ip"].split(".")[-1]),
                                  x, y, 230, bool(p.get("on", 1))))
            cb.setToolTip_(p["ip"])       # identity: the list can be rebuilt under these controls
            self.cb_pj.append(cb)
            if i % 2 == 1: y -= 28
        if len(pjs) % 2 == 1: y -= 28
        if not pjs:
            add(_lbl("none registered - use Manage…", 30, y, 300, 18)); y -= 28
        y -= 6

        # source display
        add(_lbl("Source display", 20, y, 200, 18, bold=True)); y -= 26
        self.disp = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(30, y, 300, 26), False)
        add(self.disp)
        self._populate_displays(select=int(st.get("display", 0)))
        self.cb_letter = add(self._switch("Letterbox", 350, y, 150, bool(st.get("letterbox", 1))))
        y -= 30
        self.cb_vdisplay = self._switch("Virtual 1024x768 display  (turn OFF Display Mirroring first)",
                                        30, y, 470, bool(st.get("vdisplay", 0)))
        self.cb_vdisplay.setTarget_(self); self.cb_vdisplay.setAction_("toggleVirtualDisplay:")
        add(self.cb_vdisplay)
        y -= 40
        if st.get("vdisplay", 0):                 # remembered ON -> re-arm it (auto-selects it as source)
            self._start_vdisplay()

        # start / pause / stop
        self.btn_start = self._button("Start Mirror", 30, y, 180, "start:")
        self.btn_pause = self._button("Pause", 220, y, 130, "pause:")
        self.btn_stop  = self._button("Stop", 360, y, 130, "stop:")
        add(self.btn_start); add(self.btn_pause); add(self.btn_stop)
        self.btn_pause.setEnabled_(self.mirroring)
        if self.pause_event.is_set(): self.btn_pause.setTitle_("Resume")
        y -= 44

        # picture mode (restored from settings). Quality controls are the projector's own menu.
        add(_lbl("Mode", 20, y, 200, 18, bold=True)); y -= 30
        self.seg_mode = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(30, y, 300, 26))
        self.seg_mode.setSegmentCount_(2)
        self.seg_mode.setLabel_forSegment_(MODES["hq"]["label"], 0)
        self.seg_mode.setLabel_forSegment_(MODES["fast"]["label"], 1)
        self.seg_mode.setTarget_(self); self.seg_mode.setAction_("modeChanged:")
        self.mode = st.get("mode", "fast") if st.get("mode") in MODES else "fast"
        self.seg_mode.setSelectedSegment_(0 if self.mode == "hq" else 1)
        self.seg_mode.setToolTip_("HQ (q100): best quality, ~4 fps -- slides & stills.  Adaptive (default): q50-100 -- video, more fps under load.")
        add(self.seg_mode)
        y -= 44
        self._apply_mode(self.mode)

        # projector control
        add(_lbl("Projector control", 20, y, 200, 18, bold=True)); y -= 26
        add(self._button("Power On",  30, y, 110, "powerOn:"))
        add(self._button("Power Off", 150, y, 110, "powerOff:"))
        add(self._button("Network In",270, y, 110, "netInput:"))
        self.btn_blank = self._button("Blank", 390, y, 110, "toggleBlank:"); add(self.btn_blank)
        y -= 34
        # WEB Control (http://<ip>/) is served by the projector and works. WM's separate "Remote
        # control" (cgi-bin/remocon.cgi) is NOT supported on the F300NT family - it 404s - so the
        # button is removed rather than link to a faulty page (see docs/COMPATIBILITY.md).
        add(self._button("WEB Control", 30, y, 300, "webControl:"))
        y -= 30
        self.cb_remote = self._switch("Remote control API (Stream Deck / Companion)  -  port 8765",
                                      30, y, 470, bool(st.get("remote", 0)))
        self.cb_remote.setTarget_(self); self.cb_remote.setAction_("toggleRemote:")
        add(self.cb_remote)
        y -= 40
        if st.get("remote", 0):
            self._start_control()

        # log
        add(_lbl("Status", 20, y, 200, 18, bold=True))
        self.status = add(_lbl("idle", 190, y, 310, 18, right=True))
        y -= 8
        sv = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, W-40, y-20))
        sv.setHasVerticalScroller_(True); sv.setBorderType_(1)
        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W-40, y-20))
        tv.setEditable_(False); tv.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        tv.setTextColor_(NSColor.labelColor())
        sv.setDocumentView_(tv); add(sv)
        self.logview = tv

        self._build_menu()
        if old is not None:
            old.orderOut_(None)     # orderOut, not close: closing the last window quits the app

    @objc.python_method
    def _rebuild_window(self):
        """Redraw the panel after the registered projector list changed."""
        self._save_settings()
        self._build_window()
        self.win.makeKeyAndOrderFront_(None)

    # ---------- widget factories ----------
    @objc.python_method
    def _switch(self, title, x, y, w, on):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
        b.setButtonType_(NSButtonTypeSwitch); b.setTitle_(title); b.setState_(1 if on else 0)
        return b
    @objc.python_method
    def _button(self, title, x, y, w, action):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 30))
        b.setTitle_(title); b.setBezelStyle_(NSBezelStyleRounded)
        b.setTarget_(self); b.setAction_(action); return b
    @objc.python_method

    def showAfterPrompt_(self, timer):
        try:
            if perm_preflight():
                return                      # granted meanwhile; checkPermission_ will relaunch
            self.win.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            self._permission_alert()
        except Exception as e:
            LOG.error("showAfterPrompt failed: %s" % e)

    def checkPermission_(self, timer):
        cg = _coregraphics()
        if cg is None: return
        try: ok = bool(cg.CGPreflightScreenCaptureAccess())
        except Exception: return
        if ok:
            timer.invalidate()
            self.log("Screen Recording granted - restarting to apply it...")
            self._relaunch()

    @objc.python_method
    def _relaunch(self):
        import subprocess
        from Foundation import NSBundle
        try: self._save_settings()
        except Exception: pass
        p = NSBundle.mainBundle().bundlePath()
        try: subprocess.Popen(["/bin/sh", "-c", "sleep 1; open -n %s" % json.dumps(p)])
        except Exception: pass
        NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _ensure_permission(self):
        app = NSApplication.sharedApplication()
        pre = perm_preflight()
        LOG.info("permission preflight=%s" % pre)
        if pre:
            self.log("Screen Recording: granted.")
            self._capture_selftest()
            return True
        try: app.activateIgnoringOtherApps_(True)   # so the prompt is not buried behind windows
        except Exception: pass
        # NOTE: CGRequestScreenCaptureAccess returns the CURRENT status immediately and raises
        # the prompt asynchronously - False here does NOT mean "no prompt". Never tccutil-reset
        # right after it: that deletes the entry the request just created and kills the prompt.
        got = perm_request()
        LOG.info("permission request=%s (async; prompt may still be on screen)" % got)
        if got:
            self.log("Screen Recording: granted."); self._capture_selftest(); return True
        self.log("Screen Recording not granted yet - approve it; the app restarts itself.")
        return False

    @objc.python_method
    def _capture_selftest(self):
        """Permission can report granted while capture is actually broken; prove it for real."""
        cap = capture_capable()
        LOG.info("capture capability (other apps' window titles visible) = %s" % cap)
        if cap is False:
            self.log("WARNING: permission says granted but real capture is BLOCKED "
                     "(window titles hidden -> you would mirror wallpaper only). "
                     "Use the app menu > Reset Screen Recording Permission.")
        elif cap is True:
            self.log("Capture verified: real screen content is visible.")
        try:
            img = fm.grab_screen(mon=1)
            LOG.info("grab self-test: %s", img.size if img is not None else "None")
        except Exception as e:
            LOG.error("grab self-test error: %s" % e)

    def permissionMenu_(self, sender):
        self._permission_alert()

    @objc.python_method
    def _perm_alert_response(self, r):
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL
            import subprocess
            from Foundation import NSBundle
            if r == 1000:
                NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"))
            elif r == 1001:                 # reveal so it can be dragged straight into the list
                subprocess.Popen(["/usr/bin/open", "-R", NSBundle.mainBundle().bundlePath()])
            elif r == 1002:
                perm_reset()
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                LOG.info("permission re-request after manual reset=%s" % perm_request())
        except Exception as e:
            LOG.error("permission alert response failed: %s" % e)

    @objc.python_method
    def _permission_alert(self):
        try:
            from AppKit import NSAlert, NSWorkspace
            from Foundation import NSURL
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            a = NSAlert.alloc().init()
            a.setMessageText_("Screen Recording permission needed")
            a.setInformativeText_(
                "This app cannot capture the screen yet.\n\n"
                "Turn it ON under Privacy & Security > Screen Recording.\n"
                "The app restarts itself as soon as you do.\n\n"
                "If it is already switched on, the entry is stale - "
                "use Reset Permission & Retry.\n"
                "To add it by hand: Reveal App in Finder, then drag it into the list.")
            a.addButtonWithTitle_("Open Screen Recording Settings")
            a.addButtonWithTitle_("Reveal App in Finder")
            a.addButtonWithTitle_("Reset Permission & Retry")
            a.addButtonWithTitle_("Continue Anyway")
            win = getattr(self, "win", None)
            if win is not None:
                # sheet = non-blocking, so the grant-watcher timer keeps running behind it
                a.beginSheetModalForWindow_completionHandler_(win, self._perm_alert_response)
            else:
                self._perm_alert_response(a.runModal())
        except Exception as e:
            LOG.error("permission alert failed: %s" % e)

    @objc.python_method
    def _build_menu(self):
        app = NSApplication.sharedApplication()
        mainMenu = NSMenu.alloc().init()
        appItem = NSMenuItem.alloc().init(); mainMenu.addItem_(appItem)
        app.setMainMenu_(mainMenu)
        m = NSMenu.alloc().init()
        m.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Hide", "hide:", "h"))
        m.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Reset Screen Recording Permission...", "permissionMenu:", ""))
        m.addItem_(NSMenuItem.separatorItem())
        m.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Projection Mirror", "terminate:", "q"))
        appItem.setSubmenu_(m)

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return True

    @objc.python_method
    def _load_settings(self):
        try:
            with open(SETTINGS_PATH) as fh: return json.load(fh)
        except Exception: return {}

    @objc.python_method
    def _sync_registry_from_ui(self):
        """Fold the checkbox states back into the registered list before it is saved.

        Matched by IP, never by position: the list is rebuilt under these controls whenever a
        projector is added or removed, so a positional zip would apply the wrong tick to the
        wrong projector."""
        if self.projectors is None:      # nothing built yet; nothing to fold back
            return []
        states = {}
        for cb in getattr(self, "cb_pj", []):
            try: states[str(cb.toolTip() or "")] = 1 if cb.state() else 0
            except Exception: pass
        for p in self.projectors:
            if p["ip"] in states:
                p["on"] = states[p["ip"]]
        self.projectors = reg.normalize(self.projectors)
        return self.projectors

    @objc.python_method
    def _save_settings(self):
        try:
            d = {"mode": getattr(self, "mode", "fast"),
                 "projectors": self._sync_registry_from_ui(),
                 "display": int(self.disp.indexOfSelectedItem()), "letterbox": int(self.cb_letter.state()),
                 "vdisplay": int(self.cb_vdisplay.state()),
                 "remote": int(self.cb_remote.state())}
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            with open(SETTINGS_PATH, "w") as fh: json.dump(d, fh, indent=2)
        except Exception: pass


    @objc.python_method
    def _populate_displays(self, select=None):
        """(Re)fill the source-display popup from the live monitor list. Called at build and after a
        virtual display is enabled/disabled so the new display appears."""
        keep = self.disp.indexOfSelectedItem() if self.disp.numberOfItems() else 0
        self.disp.removeAllItems()
        try:
            for (i, label, w, h) in fm.list_monitors():
                self.disp.addItemWithTitle_("%d: %s  (%dx%d)" % (i, label, w, h))
        except Exception as e:
            self.disp.addItemWithTitle_("1: main"); self.log("monitor list error: %s" % e)
        idx = keep if select is None else select
        if idx is not None and 0 <= idx < self.disp.numberOfItems():
            self.disp.selectItemAtIndex_(idx)

    def toggleVirtualDisplay_(self, sender):
        if sender.state(): self._start_vdisplay()
        else: self._stop_vdisplay()
        self._save_settings()                     # remember the choice across restarts

    @objc.python_method
    def _start_vdisplay(self, w=1024, h=768):
        import subprocess
        if getattr(self, "_vd_proc", None): return
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--vdisplay-hold", str(w), str(h)]
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            cmd = [sys.executable, os.path.join(here, "pm_vdisplay.py"), "hold", str(w), str(h)]
        try:
            self._vd_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True)
        except Exception as e:
            self.log("virtual display failed to start: %s" % e)
            self._on_main(lambda: self.cb_vdisplay.setState_(0)); return
        self.log("virtual display: starting %dx%d ..." % (w, h))
        def waiter():
            line = ""
            try:
                for _ in range(80):                 # up to ~ readline-bounded wait for READY
                    line = self._vd_proc.stdout.readline()
                    if not line or line.startswith("READY"): break
            except Exception: pass
            self.log("virtual display: %s" % (line.strip() or "no READY line"))
            self._on_main(lambda: self._refresh_displays(select_new=True))
        threading.Thread(target=waiter, name="vd-wait", daemon=True).start()

    @objc.python_method
    def _stop_vdisplay(self):
        p = getattr(self, "_vd_proc", None)
        if p:
            try: p.terminate()
            except Exception: pass
            self._vd_proc = None
            self.log("virtual display: stopped")
        self._on_main(lambda: self._refresh_displays(select_new=False))

    @objc.python_method
    def _refresh_displays(self, select_new=False):
        # select the highest-index display (the freshly added virtual one) when enabling
        self._populate_displays()
        if select_new and self.disp.numberOfItems() > 0:
            self.disp.selectItemAtIndex_(self.disp.numberOfItems() - 1)
        self.log("source displays refreshed (%d)" % self.disp.numberOfItems())

    @objc.python_method
    def _apply_mode(self, mode):
        m = MODES[mode]; self.mode = mode
        with fm._PARAM_LOCK:
            fm.JFIF = True; fm.JFIF_QMIN = m["qmin"]; fm.JFIF_QMAX = m["qmax"]; fm._ADAPT_Q = m["q"]
            fm.QPIN = None                       # modes own quality; drop any leftover env/test pin
        sess = getattr(self, "mirror_session", None)
        if sess is not None:
            sess.set_mode(min_interval=m["interval"], governor=m["governor"],
                          target_kbps=(ADAPTIVE_KBPS if m["governor"] else 0.0))
        LOG.info("mode %s: q%d-%d start q%d, floor %.2fs, governor=%s",
                 mode, m["qmin"], m["qmax"], m["q"], m["interval"], m["governor"])

    # ---------- actions ----------
    def modeChanged_(self, sender):
        self._apply_mode("hq" if sender.selectedSegment() == 0 else "fast")
        self._save_settings()

    def applicationWillTerminate_(self, note):
        self._terminating = True
        try: self._stop_vdisplay()
        except Exception: pass
        try: self._stop_control()
        except Exception: pass          # _on_main drops late UI updates from worker threads
        try: self.stop_event.set()
        except Exception: pass
        try:                                        # let the graceful close finish (blank -> video
            t = getattr(self, "thread", None)       # FIN -> QUIT/182 OK per sender) instead of being
            if t is not None and t.is_alive(): t.join(timeout=6.0)   # killed mid-strip on process exit
        except Exception: pass
        try: self._save_settings()
        except Exception: pass

    @objc.python_method
    def _selected_ips(self):
        return reg.selected_ips(self._sync_registry_from_ui())

    @objc.python_method
    def _target_ips(self):
        """Projectors a control command applies to: the ticked ones, else every registered one."""
        return self._selected_ips() or reg.all_ips(self.projectors)

    def start_(self, sender):
        ips = self._selected_ips()
        if not ips: self.log("select at least one projector"); return
        _t = getattr(self, "thread", None)
        if _t is not None and _t.is_alive() and not self.mirroring:
            self.log("mirror is still starting - ignoring duplicate start"); return
        if self.mirroring:
            self._apply_selection(ips)   # add/remove live; don't disturb the running ones
            return
        mon = self.disp.indexOfSelectedItem() + 1
        letter = bool(self.cb_letter.state())
        self._save_settings()
        self.stop_event.clear()
        self.pause_event.clear()
        self.btn_pause.setTitle_("Pause"); self.btn_pause.setEnabled_(True)
        self.thread = threading.Thread(target=self._mirror, args=(ips, letter, mon), daemon=True)
        self.thread.start()

    def stop_(self, sender):
        if not self.mirroring: self.log("not mirroring"); return
        self.log("Stopping...")
        self.stop_event.set()

    def toggleBlank_(self, sender):
        ips = self._target_ips()
        def run():
            muted = fm.pj_is_muted(ips[0])          # real state, not a local flag
            if muted is None:
                self.log("%s blank: no PJLink response (offline?)" % ips[0]); return
            target = not muted                       # drive every selected projector to the same state
            for ip in ips:
                cur = fm.pj_is_muted(ip)
                if cur == target:
                    self.log("%s already %s" % (ip, "blanked" if target else "unblanked")); continue
                r = fm.pj_blank(ip) if target else fm.pj_unblank(ip)
                self.log("%s blank %s -> %s" % (ip, "on" if target else "off", r))
            self.blanked = target
            self._on_main(lambda: self.btn_blank.setTitle_("Unblank" if target else "Blank"))
        threading.Thread(target=run, daemon=True).start()

    @objc.python_method
    def _pj(self, ips, key, want, cmd, name):
        """Read current state FIRST; only send the command if it is actually needed."""
        def run(ip):
            cur = fm.pj_state(ip).get(key)
            if cur is None:
                self.log("%s %s: no PJLink response (offline?)" % (ip, name)); return
            if cur == want:
                self.log("%s already %s (%s=%s)" % (ip, name, key, cur)); return
            self.log("%s %s: %s=%s -> %s" % (ip, name, key, cur, fm.pjlink(ip, cmd)))
        for ip in ips:
            threading.Thread(target=run, args=(ip,), daemon=True).start()

    def powerOn_(self, s):  self._pj(self._target_ips(), "power", "1",  "%1POWR 1", "power on")
    def powerOff_(self, s):
        # Stop presenting on the target projectors FIRST. Otherwise the power-off drops the video,
        # the worker auto-reconnects, and _connect_with_retry -> power_on_network powers the
        # projector straight back on - Power Off fighting itself. A user expects Power Off to stop
        # the stream and stay off.
        ips = self._target_ips()
        sess = getattr(self, "mirror_session", None)
        if self.mirroring and sess is not None:
            remaining = [p for p in list(getattr(sess, "ips", [])) if p not in ips]
            if any(p in ips for p in getattr(sess, "ips", [])):
                if remaining:
                    sess.set_wanted(remaining)
                    self.log("power off: stopped presenting on %s first" % ", ".join(ips))
                else:
                    self.log("power off: stopping the mirror first")
                    self.stop_(None)
        self._pj(ips, "power", "0", "%1POWR 0", "power off")
    def netInput_(self, s): self._pj(self._target_ips(), "input", "51", "%1INPT 51", "network input")

    # ---------- browser control (what WM's "Remote control" / "WEB control" actually are) ----------
    @objc.python_method
    def _open_urls(self, urls):
        ws = NSWorkspace.sharedWorkspace()
        for u in urls:
            self.log("opening %s" % u)
            try: ws.openURL_(NSURL.URLWithString_(u))
            except Exception as e: self.log("  could not open %s: %s" % (u, e))

    # ---------- remote control API (V2f) ----------
    def toggleRemote_(self, sender):
        if sender.state(): self._start_control()
        else: self._stop_control()
        self._save_settings()                     # remember the choice across restarts

    @objc.python_method
    def _start_control(self, port=8765):
        if getattr(self, "_ctl_server", None) is not None: return
        try:
            self._ctl_server = ctlsrv.ControlServer(self.ctl_dispatch, port=port)
            url = self._ctl_server.start()
            self.log("Remote control ON: %s  (LAN only; routes: /status /start /stop /pause /resume "
                     "/mode?m=fast|hq /vdisplay?on=1|0 /blank?on=1|0 /power?on=1|0 /input)" % url)
        except Exception as e:
            self._ctl_server = None; self.log("Remote control failed: %s" % e)
            self._on_main(lambda: self.cb_remote.setState_(0))

    @objc.python_method
    def _stop_control(self):
        srv = getattr(self, "_ctl_server", None)
        if srv:
            srv.stop(); self._ctl_server = None; self.log("Remote control OFF")

    @objc.python_method
    def _run_main_sync(self, fn, timeout=4.0):
        """Run fn on the main thread and BLOCK until it finishes, so the API response reflects the
        resulting state (full feedback)."""
        import threading as _th
        done = _th.Event(); box = {}
        def wrap():
            try: box["r"] = fn()
            except Exception as e: box["e"] = e
            finally: done.set()
        self._on_main(wrap)
        if not done.wait(timeout):
            box["timeout"] = True            # main thread still busy: the action has NOT completed
        return box

    @objc.python_method
    def ctl_dispatch(self, path, q):
        # runs on the HTTP thread; actions are marshalled to the main thread and awaited
        if path == "/status":
            return 200, self._ctl_status()
        routes = {
            "/start":  lambda: self.start_(None),
            "/stop":   lambda: self.stop_(None),
            "/pause":  lambda: self._ctl_pause(True),
            "/resume": lambda: self._ctl_pause(False),
            "/power":  lambda: (self.powerOn_ if q.get("on", "1") == "1" else self.powerOff_)(None),
            "/input":  lambda: self.netInput_(None),
            "/blank":  lambda: self._ctl_blank(q.get("on", "1") == "1"),
            "/mode":   lambda: self._ctl_mode(q.get("m", "fast")),
            "/vdisplay": lambda: self._ctl_vdisplay(q.get("on", "1") == "1"),
        }
        fn = routes.get(path)
        if fn is None:
            return 404, {"error": "unknown path", "paths": sorted(list(routes) + ["/status"])}
        self.log("remote: %s%s" % (path, (" " + str(q)) if q else ""))
        box = self._run_main_sync(fn)                 # await the action, then report full state
        st = self._ctl_status()
        if box.get("timeout"):
            return 504, {"ok": False, "path": path, "error": "action timed out (main thread busy)", "status": st}
        if "e" in box:
            return 500, {"ok": False, "path": path, "error": str(box["e"]), "status": st}
        return 200, {"ok": True, "path": path, "query": q, "status": st}

    @objc.python_method
    def _ctl_status(self):
        try:                                   # _selected_ips reads NSButtons -> MUST run on the main
            _b = self._run_main_sync(self._selected_ips, timeout=2.0)   # thread, not the HTTP worker
            ips = _b.get("r") or []
        except Exception: ips = []
        rates = {}
        try:
            _sess = getattr(self, "mirror_session", None)
            if _sess is not None:
                for ip, v in _sess.summary().items(): rates[ip] = v.get("sent", 0)
        except Exception: pass
        return {"version": VERSION, "mirroring": bool(self.mirroring),
                "paused": bool(self.pause_event.is_set()), "mode": getattr(self, "mode", "fast"),
                "virtual_display": bool(getattr(self, "_vd_proc", None)),
                "subsampling": getattr(fm, "SUBSAMPLING", 2),
                "quality": (fm.QPIN if getattr(fm, "QPIN", None) is not None else "adaptive"),
                "projectors": ips, "sent": rates}

    @objc.python_method
    def _ctl_pause(self, want):
        if want and not self.pause_event.is_set(): self.pause_(None)
        elif not want and self.pause_event.is_set(): self.pause_(None)

    @objc.python_method
    def _ctl_mode(self, m):
        if m not in MODES: m = "fast"
        try: self.seg_mode.setSelectedSegment_(0 if m == "hq" else 1)
        except Exception: pass
        self._apply_mode(m); self._save_settings()

    @objc.python_method
    def _ctl_vdisplay(self, on):
        try: self.cb_vdisplay.setState_(1 if on else 0)
        except Exception: pass
        (self._start_vdisplay if on else self._stop_vdisplay)()
        self._save_settings()

    @objc.python_method
    def _ctl_blank(self, on):
        if bool(getattr(self, "blanked", False)) != bool(on):
            self.toggleBlank_(None)

    def webControl_(self, sender):
        ips = self._target_ips()
        if not ips: self.log("no projectors registered"); return
        self._open_urls([fm.web_url(ip) for ip in ips])

    # ---------- pause ----------
    def pause_(self, sender):
        """Freeze the wall on the current frame, keeping the session open.

        Only the CAPTURE side stops. The session stays open on its own: every Sender runs a 4s
        NOOP keepalive thread (fm.Sender._ka_loop) whether or not frames flow, and the 3s keyframe
        keeps re-sending the frozen frame so a stale band still repairs itself. Tearing the
        session down and re-associating on resume is what wedges the network module."""
        if not self.mirroring:
            self.log("not mirroring"); return
        sess = getattr(self, "mirror_session", None)
        if self.pause_event.is_set():
            self.pause_event.clear()
            if sess: sess.resume()
            self.btn_pause.setTitle_("Pause"); self.log("Resumed.")
        else:
            self.pause_event.set()
            if sess: sess.pause()
            self.btn_pause.setTitle_("Resume")
            self.log("Paused - the wall holds this frame; the session stays open.")

    # ---------- registered projectors ----------
    def manageProjectors_(self, sender):
        if self.mirroring:
            self.log("stop the mirror before changing the projector list"); return
        self._sync_registry_from_ui()
        self._build_manager()

    @objc.python_method
    def _mgr_close(self):
        if self.mgr_win is not None:
            try: self.mgr_win.orderOut_(None)
            except Exception: pass
            self.mgr_win = None

    @objc.python_method
    def _build_manager(self):
        pjs = self.projectors
        rowh = 30
        W = 470
        H = 150 + rowh * max(1, len(pjs))
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False)
        win.setTitle_("Registered Projectors")
        win.center()
        old = self.mgr_win
        self.mgr_win = win
        v = win.contentView()
        def add(c): v.addSubview_(c); return c

        y = H - 36
        add(_lbl("Registered Projectors", 20, y, 300, 20, bold=True, size=14)); y -= 26
        add(_lbl("Any F300-series unit on the subnet works - its key is derived from its own "
                 "discovery reply.", 20, y, W - 40, 16)); y -= 24

        self._mgr_fields = []
        if not pjs:
            add(_lbl("none yet - use Find Projectors, or Add by IP", 30, y, 300, 18)); y -= rowh
        for p in pjs:
            f = NSTextField.alloc().initWithFrame_(NSMakeRect(30, y, 190, 22))
            f.setStringValue_(p["name"]); add(f)
            self._mgr_fields.append((p["ip"], f))
            add(_lbl(p["ip"], 232, y + 2, 130, 18))
            b = self._button("Remove", 360, y - 4, 90, "removeProjector:")
            b.setToolTip_(p["ip"])            # the row identity; titles are user-editable
            add(b)
            y -= rowh
        y -= 6
        add(self._button("Find Projectors", 30, y, 150, "findProjectors:"))
        add(self._button("Add by IP…", 190, y, 120, "addProjector:"))
        add(self._button("Save Names", 320, y, 130, "saveNames:"))
        win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        if old is not None:
            try: old.orderOut_(None)
            except Exception: pass

    @objc.python_method
    def _commit_names(self):
        for ip, field in self._mgr_fields:
            try: self.projectors = reg.rename(self.projectors, ip, field.stringValue())
            except Exception: pass

    def saveNames_(self, sender):
        self._commit_names()
        self._rebuild_window()
        self._build_manager()
        self.log("projector names saved")

    def removeProjector_(self, sender):
        ip = sender.toolTip()
        self._commit_names()
        self.projectors = reg.remove(self.projectors, ip)
        self.log("removed %s from the registered list" % ip)
        self._rebuild_window()
        self._build_manager()

    def addProjector_(self, sender):
        from AppKit import NSAlert
        self._commit_names()
        a = NSAlert.alloc().init()
        a.setMessageText_("Add a projector")
        a.setInformativeText_("Its IP address on the LAN, e.g. 192.168.1.102.")
        fld = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 220, 24))
        a.setAccessoryView_(fld)
        a.addButtonWithTitle_("Add"); a.addButtonWithTitle_("Cancel")
        if a.runModal() != 1000:
            return
        try:
            self.projectors, added = reg.add(self.projectors, fld.stringValue())
        except ValueError as e:
            self.log("add failed: %s" % e); return
        self.log(("added %s" if added else "%s is already registered") % fld.stringValue().strip())
        self._rebuild_window()
        self._build_manager()

    def findProjectors_(self, sender):
        """Broadcast discovery only - no session is opened, so this cannot wedge a projector."""
        self._commit_names()
        self.log("searching for projectors ...")
        def run():
            try:
                found = fm.discover_all(timeout=3.5, hint_ips=reg.all_ips(self.projectors))
            except Exception as e:
                self._on_main(lambda: self.log("discovery failed: %s" % e)); return
            def done():
                if not found:
                    self.log("no projectors answered (powered on? same subnet?)"); return
                for i in found:
                    self.log("  found %s  %s  %s" % (i["ip"], i.get("name", ""),
                                                     (i.get("location") or "").strip()))
                self.projectors, added = reg.merge_discovered(self.projectors, found)
                self.log("registered %d new" % len(added) if added
                         else "all discovered projectors were already registered")
                self._rebuild_window()
                self._build_manager()
            self._on_main(done)
        threading.Thread(target=run, daemon=True).start()

    # ---------- mirror loop (background thread) ----------
    @objc.python_method
    def _apply_selection(self, ips):
        """Start pressed while already mirroring -> reconcile the projector set in place."""
        sess = getattr(self, "mirror_session", None)
        if sess is None:
            self.log("already mirroring"); return
        sess.set_wanted(list(ips))
        self.log("projector selection -> %s" % ", ".join(sorted(ips)))

    @objc.python_method
    @objc.python_method
    def _begin_activity(self):
        """Tell macOS this app is doing latency-critical user-initiated work, so App Nap / timer
        coalescing / idle display sleep don't throttle the producer+sender loops mid-service."""
        try:
            self._activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                NS_ACTIVITY_OPTS, "Mirroring to projectors")
        except Exception:
            self._activity = None

    @objc.python_method
    def _end_activity(self):
        try:
            a = getattr(self, "_activity", None)
            if a is not None:
                NSProcessInfo.processInfo().endActivity_(a)
        except Exception:
            pass
        finally:
            self._activity = None

    @objc.python_method
    def _mirror(self, ips, letterbox, mon):
        # V2.4: the GUI drives the shared MirrorSession (one code path with the CLI/soak harness).
        m = MODES[self.mode]
        did = fm.cg_display_for_mss_monitor(mon)
        sess = fs.MirrorSession(ips, (lambda: fm.grab_screen(mon=mon)), log=self.log,
                                min_interval=m["interval"], letterbox=letterbox, auto_reconnect=True,
                                capture=("stream" if did else "poll"), display_id=(did or 1))
        sess.governor = m["governor"]
        sess.target_kbps = ADAPTIVE_KBPS if m["governor"] else 0.0
        sess.report_sec = 30        # periodic throughput line in the log (restored in v2.4)
        self.mirror_session = sess
        self.log("Mirroring %s (display %d), mode=%s" % (list(ips), mon, self.mode))
        if not sess.start():
            self.log("No projectors connected.")
            self.mirror_session = None; self.mirroring = False
            self._on_main(lambda: (self.btn_pause.setEnabled_(False),))
            return
        self.blanked = False; self.mirroring = True
        self._begin_activity()                 # hold macOS active so App Nap can't throttle streaming
        self._on_main(lambda: (self.btn_pause.setTitle_("Pause"), self.btn_pause.setEnabled_(True)))
        self.stop_event.wait()                 # MirrorSession runs its own producer + senders
        sess.stop()
        self._end_activity()
        self.mirror_session = None; self.mirroring = False; self.pause_event.clear()
        self._fps_last = {}
        self._on_main(lambda: (self.btn_pause.setTitle_("Pause"), self.btn_pause.setEnabled_(False)))
        self.log("Stopped.")


def main():
    if "--beacon-hold" in sys.argv:              # spawned child: just hold a projector's OSD beacon
        i = sys.argv.index("--beacon-hold")
        fm.beacon_hold(sys.argv[i+1], sys.argv[i+2], sys.argv[i+3], sys.argv[i+4], float(sys.argv[i+5])); return
    if "--vdisplay-hold" in sys.argv:            # spawned child: just hold a virtual display
        i = sys.argv.index("--vdisplay-hold")
        w = int(sys.argv[i+1]) if len(sys.argv) > i+1 else 1024
        h = int(sys.argv[i+2]) if len(sys.argv) > i+2 else 768
        vdisp.hold(w, h); return
    sys.stdout = _StreamToLog(logging.INFO)     # engine print() -> rotating log
    sys.stderr = _StreamToLog(logging.ERROR)
    LOG.info("=== app start === v%s", VERSION)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    d = Delegate.alloc().init()
    app.setDelegate_(d)
    app.run()

if __name__ == "__main__":
    main()
