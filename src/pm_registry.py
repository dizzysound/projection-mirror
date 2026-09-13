#!/usr/bin/env python3
"""
pm_registry.py - the registered-projector list (Wireless Manager's "Registered Projectors").

Pure Python: no AppKit, no sockets. That is deliberate - the list is the one part of the app a
user changes, so it has to be testable offline (test_offline.py).

Until v1.4 the two projectors were baked into pm_app.py as LEFT/RIGHT constants, so a
replaced or added unit meant a code edit. Since the key derivation is data-driven (pm_pkey.py) the
association key is DERIVED from a projector's own 602 discovery reply, so any unit on the subnet
works with no code change - this module is what lets the UI say so.

Entry: {"ip": "192.168.1.100", "name": "Projector 1", "on": 1}
"""
import re

# Example placeholder projectors. Used only to seed a first run / migrate a pre-v1.5 settings file.
# Replace with your own units (Manage... > Find Projectors discovers them on the subnet).
DEFAULT_PROJECTORS = [
    {"ip": "192.168.1.100", "name": "Projector 1", "on": 1},
    {"ip": "192.168.1.101", "name": "Projector 2", "on": 1},
]

_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def valid_ip(s):
    m = _IPV4.match(str(s or "").strip())
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


def clean_name(name, ip=""):
    """Projector names go in a window and a settings file; keep them short and printable.
    WM caps its own registered names at 16 characters."""
    n = " ".join(str(name or "").split())
    n = "".join(c for c in n if c.isprintable())[:24]
    return n or (str(ip) or "projector")


def normalize(entries):
    """Drop malformed rows, de-duplicate by IP (first wins), coerce the 'on' flag."""
    out, seen = [], set()
    for e in entries or []:
        try:
            ip = str(e.get("ip", "")).strip()
        except AttributeError:
            continue
        if not valid_ip(ip) or ip in seen:
            continue
        seen.add(ip)
        out.append({"ip": ip, "name": clean_name(e.get("name"), ip),
                    "on": 1 if e.get("on", 1) else 0})
    return out


def from_settings(st):
    """Read the projector list out of settings.json, migrating a pre-v1.5 file.

    Before v1.5 the file only held `left` / `right` checkbox states for the two baked IPs; keep
    those states so an upgrade does not silently re-enable a projector the user had switched off.
    """
    st = st or {}
    if "projectors" in st:
        # Present but empty means the user removed them all - honour that rather than helpfully
        # re-seeding the placeholder units behind their back. Manage… > Find Projectors rebuilds it.
        return normalize(st["projectors"])
    seeded = []
    for i, d in enumerate(DEFAULT_PROJECTORS):
        key = "left" if i == 0 else "right"   # pre-v1.5 held only left/right flags for two units
        seeded.append(dict(d, on=1 if st.get(key, 1) else 0))
    return normalize(seeded)


def add(entries, ip, name=None):
    """Add one projector. Returns (entries, added) - added is False if the IP is already there."""
    entries = normalize(entries)
    ip = str(ip or "").strip()
    if not valid_ip(ip):
        raise ValueError("not an IPv4 address: %r" % ip)
    if any(e["ip"] == ip for e in entries):
        return entries, False
    entries.append({"ip": ip, "name": clean_name(name, ip), "on": 1})
    return entries, True


def remove(entries, ip):
    return [e for e in normalize(entries) if e["ip"] != str(ip).strip()]


def rename(entries, ip, name):
    entries = normalize(entries)
    for e in entries:
        if e["ip"] == str(ip).strip():
            e["name"] = clean_name(name, e["ip"])
    return entries


def name_for_discovery(info, ip):
    """A readable name from a 602 reply: its LOCATION field ("RIGHT"), else its "ProjNNNN" name."""
    info = info or {}
    loc = str(info.get("location", "")).strip()
    if loc:
        return clean_name(loc.title(), ip)
    return clean_name(info.get("name") or "", ip)


def merge_discovered(entries, found):
    """Fold discovery results into the list. Existing rows keep their name and on/off state -
    a discovery sweep must never re-enable a projector the user switched off, or rename one
    they renamed. Returns (entries, added_ips)."""
    entries = normalize(entries)
    have = {e["ip"] for e in entries}
    added = []
    for info in found or []:
        ip = str((info or {}).get("ip", "")).strip()
        if not valid_ip(ip) or ip in have:
            continue
        entries.append({"ip": ip, "name": name_for_discovery(info, ip), "on": 1})
        have.add(ip)
        added.append(ip)
    return entries, added


def selected_ips(entries):
    return [e["ip"] for e in normalize(entries) if e["on"]]


def all_ips(entries):
    return [e["ip"] for e in normalize(entries)]
