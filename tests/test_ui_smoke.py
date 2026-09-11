#!/usr/bin/env python3
"""
test_ui_smoke.py - builds the real AppKit panel without running the app.

Needs a GUI login session (it makes NSWindows); needs NO projector and NO Screen Recording,
because it calls _build_window() directly instead of going through the launch path. It exists
because the panel's layout is hand-computed and the projector rows are now DYNAMIC: a bad row
count silently pushes controls off the window rather than raising anything.

    python3 test_ui_smoke.py
"""
import sys
# PROJECT-RELATIVE MODULE PATH: work whether run from the vault (flat) or the
# repo (tests/ beside src/). Copying this file between the two must not break it.
import os as _os
_here=_os.path.dirname(_os.path.abspath(__file__))
for _c in (_here, _os.path.join(_here,'..','src'), _os.getcwd()):
    if _os.path.exists(_os.path.join(_c,'pm_mirror_v2.py')): sys.path.insert(0,_c)
from AppKit import NSApplication
import pm_app as app
import pm_registry as reg

FAIL = []
def check(name, cond, detail=""):
    print("  %-56s %s%s" % (name, "ok" if cond else "FAIL", "" if cond else "  <- " + str(detail)))
    if not cond: FAIL.append(name)

NSApplication.sharedApplication()

def build(projectors):
    d = app.Delegate.alloc().init()
    d._perm_granted = True
    d.settings = {"projectors": projectors}
    d.projectors = reg.normalize(projectors)
    d._load_settings = lambda: d.settings      # never read the user's real settings file
    d._save_settings = lambda: None
    d._build_window()
    return d

print("\n-- panel builds for any registered-list size --")
two = list(reg.DEFAULT_PROJECTORS)
for n in (0, 1, 2, 3, 5):
    pjs = [{"ip": "192.168.1.%d" % (190 + i), "name": "PJ%d" % i, "on": 1} for i in range(n)]
    d = build(pjs)
    frame = d.win.frame()
    check("%d projectors: window built, %d checkboxes" % (n, len(d.cb_pj)),
          d.win is not None and len(d.cb_pj) == n)
    check("%d projectors: every control is inside the window" % n,
          all(sv.frame().origin.y >= 0 and
              sv.frame().origin.y + sv.frame().size.height <= frame.size.height
              for sv in d.win.contentView().subviews()),
          [(sv.frame().origin.y, sv.frame().size.height)
           for sv in d.win.contentView().subviews()
           if sv.frame().origin.y < 0])

def overlaps(win):
    """Pairs of INTERACTIVE controls whose frames intersect. Static labels are ignored: the
    layout has always let a right-aligned value label share a row with its caption."""
    from AppKit import NSButton, NSSlider, NSPopUpButton, NSScrollView
    ctls = [v for v in win.contentView().subviews()
            if isinstance(v, (NSButton, NSSlider, NSPopUpButton, NSScrollView))]
    bad = []
    for i, a in enumerate(ctls):
        fa = a.frame()
        for b in ctls[i + 1:]:
            fb = b.frame()
            if (fa.origin.x < fb.origin.x + fb.size.width and
                    fb.origin.x < fa.origin.x + fa.size.width and
                    fa.origin.y < fb.origin.y + fb.size.height and
                    fb.origin.y < fa.origin.y + fa.size.height):
                bad.append((str(a.description())[:40], str(b.description())[:40]))
    return bad

print("\n-- no control sits on top of another --")
for n in (0, 1, 2, 3, 5):
    pjs = [{"ip": "192.168.1.%d" % (190 + i), "name": "PJ%d" % i, "on": 1} for i in range(n)]
    d = build(pjs)
    bad = overlaps(d.win)
    check("%d projectors: %d overlapping controls" % (n, len(bad)), not bad, bad[:2])
d = build(two); d._build_manager()
_mb = overlaps(d.mgr_win)
check("manager window: %d overlapping controls" % len(_mb), not _mb, _mb[:2])
d.mgr_win.orderOut_(None)

print("\n-- controls the new features need --")
d = build(two)
for sel in ("start:", "pause:", "stop:", "webControl:", "modeChanged:", "manageProjectors:",
            "findProjectors:", "addProjector:", "removeProjector:", "saveNames:",
            "powerOn:", "powerOff:", "netInput:", "toggleBlank:"):
    check("responds to %s" % sel, d.respondsToSelector_(sel))
check("Pause starts disabled (nothing is mirroring)", not d.btn_pause.isEnabled())
check("checkboxes carry their IP as identity",
      [str(cb.toolTip()) for cb in d.cb_pj] == [p["ip"] for p in d.projectors])

print("\n-- checkbox state folds back into the registry by IP --")
d.cb_pj[0].setState_(0); d.cb_pj[1].setState_(1)
check("selected ips follow the ticks", d._selected_ips() == [d.projectors[1]["ip"]],
      d._selected_ips())
check("target falls back to every registered projector when none is ticked",
      (d.cb_pj[1].setState_(0) or d._target_ips()) == reg.all_ips(d.projectors))
d.projectors = reg.remove(d.projectors, d.projectors[0]["ip"])   # list changes under the controls
check("a stale checkbox cannot flip the wrong projector",
      [p["ip"] for p in d._sync_registry_from_ui()] == [two[1]["ip"]])

print("\n-- modes drive the session governor target; no slider (2026-09-10) --")
class _FakeSess:
    def __init__(self): self.governor=None; self.target_kbps=-1.0; self.min_interval=None
    def set_mode(self, min_interval=None, governor=None, target_kbps=None):
        if min_interval is not None: self.min_interval = min_interval
        if governor is not None: self.governor = governor
        if target_kbps is not None: self.target_kbps = target_kbps
d = build(two)
d.mirror_session = _FakeSess()
d._apply_mode("fast")
check("Adaptive turns the governor on at the fixed target", d.mirror_session.governor is True
      and d.mirror_session.target_kbps == app.ADAPTIVE_KBPS, d.mirror_session.target_kbps)
d._apply_mode("hq")
check("HQ is governor-off with target 0 (pinned q100)",
      d.mirror_session.governor is False and d.mirror_session.target_kbps == 0.0, d.mirror_session.target_kbps)
check("the Fast fps<->quality slider is gone", not hasattr(d, "sl_fast"))
check("HQ mode pins q100", app.MODES["hq"]["qmin"] == 100 and app.MODES["hq"]["qmax"] == 100)
check("Adaptive spans q50-100 (floor q15 -> q75 -> q50 on 2026-09-11; the\n       hardware should never use, and wrecked the picture)",
      app.MODES["fast"]["qmin"] == 50 and app.MODES["fast"]["qmax"] == 100)
check("Adaptive starts at or above its floor", app.MODES["fast"]["q"] >= app.MODES["fast"]["qmin"])

print("\n-- virtual display setting persists across restarts (feature 2026-09-10) --")
import tempfile as _tmp, json as _json
# save: the settings dict carries the checkbox state
_tf = _tmp.mktemp(suffix=".json"); _orig = app.SETTINGS_PATH; app.SETTINGS_PATH = _tf
d = build(two)
d.cb_vdisplay.setState_(1)
app.Delegate._save_settings(d)                 # real save (bypass build()'s no-op override)
_saved = _json.load(open(_tf)); app.SETTINGS_PATH = _orig
check("_save_settings persists vdisplay=1", _saved.get("vdisplay") == 1, _saved)
# restore: vdisplay:1 in settings re-arms the display on launch and checks the box
_calls = []
d2 = app.Delegate.alloc().init()
d2._perm_granted = True
d2.settings = {"projectors": two, "vdisplay": 1}
d2.projectors = reg.normalize(two)
d2._load_settings = lambda: d2.settings
d2._save_settings = lambda: None
d2._start_vdisplay = lambda *a, **k: _calls.append("start")   # don't spawn a real subprocess
d2._build_window()
check("saved vdisplay=1 re-arms the display on launch", _calls == ["start"], _calls)
check("saved vdisplay=1 restores the checkbox checked", bool(d2.cb_vdisplay.state()))
# default (no saved flag) does NOT auto-start
_calls2 = []
d3 = app.Delegate.alloc().init()
d3._perm_granted = True; d3.settings = {"projectors": two}
d3.projectors = reg.normalize(two)
d3._load_settings = lambda: d3.settings; d3._save_settings = lambda: None
d3._start_vdisplay = lambda *a, **k: _calls2.append("start")
d3._build_window()
check("no saved flag leaves the virtual display off", _calls2 == [] and not d3.cb_vdisplay.state())

print("\n-- remote-control API setting persists across restarts (fix 2026-09-10) --")
# regression: terminate stops the server (_ctl_server=None) BEFORE save; saving the checkbox
# state (not the live server object) must still persist remote=1.
_tf2 = _tmp.mktemp(suffix=".json"); _o2 = app.SETTINGS_PATH; app.SETTINGS_PATH = _tf2
d = build(two)
d.cb_remote.setState_(1)
d._ctl_server = None                            # simulate the terminate-order teardown
app.Delegate._save_settings(d)
_sv = _json.load(open(_tf2)); app.SETTINGS_PATH = _o2
check("remote persists from checkbox even after the server is torn down", _sv.get("remote") == 1, _sv)
# restore: remote:1 re-arms the control server and checks the box on launch
_rc = []
d4 = app.Delegate.alloc().init()
d4._perm_granted = True; d4.settings = {"projectors": two, "remote": 1}
d4.projectors = reg.normalize(two)
d4._load_settings = lambda: d4.settings; d4._save_settings = lambda: None
d4._start_control = lambda *a, **k: _rc.append("start")   # don't bind a real socket
d4._build_window()
check("saved remote=1 re-arms the control server on launch", _rc == ["start"], _rc)
check("saved remote=1 restores the checkbox checked", bool(d4.cb_remote.state()))

print("\n-- manager window --")
d = build(two)
d._build_manager()
check("manager window built", d.mgr_win is not None)
check("one editable name field per projector", len(d._mgr_fields) == len(two))
d._mgr_fields[0][1].setStringValue_("Stage Left")
d._commit_names()
check("editing a field renames that projector", d.projectors[0]["name"] == "Stage Left",
      d.projectors[0])
d.mgr_win.orderOut_(None)

print("\n" + ("UI SMOKE PASSED" if not FAIL else "FAILURES: %s" % FAIL))
sys.exit(1 if FAIL else 0)
