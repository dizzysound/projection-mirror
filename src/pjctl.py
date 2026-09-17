#!/usr/bin/env python3
"""
pjctl.py - PJLink control for Panasonic PT-F300 projectors.

No dependencies. Standard PJLink Class 1 over TCP 4352.
The PJLink password default matches the projector family; edit the map for your units.

Examples:
    ./pjctl.py status              # power/input/lamp/errors for both
    ./pjctl.py on                  # power on both
    ./pjctl.py off                 # power off both (asks once)
    ./pjctl.py left input network  # left -> NETWORK input
    ./pjctl.py both input rgb1     # both -> RGB1
    ./pjctl.py right mute on       # blank right
    ./pjctl.py both mute off       # unblank both
"""
import socket, hashlib, sys, time

PASSWORD = "panasonic"
PROJECTORS = {
    "left":  "192.168.1.100",   # edit these to your units' IPs
    "right": "192.168.1.101",
}
INPUTS = {                      # friendly name -> PJLink code
    "rgb1": "11", "rgb2": "12",
    "video1": "21", "video2": "22",
    "digital1": "31", "hdmi": "31",
    "network": "51", "net": "51",
}
INPUT_NAMES = {"11":"RGB1","12":"RGB2","21":"VIDEO1","22":"VIDEO2","31":"DIGITAL1","51":"NETWORK"}
POWER = {"0":"OFF","1":"ON","2":"COOLING","3":"WARMING"}

# PJLink response codes (spec v1.04 s2.3/s2.4), rendered for a human.
ERRORS = {
    "ERR1": "projector does not support that command",
    "ERR2": "out of parameter (value not valid for this projector)",
    "ERR3": "unavailable right now - the projector is warming up, cooling down, or in standby",
    "ERR4": "projector reports a hardware failure",
}

def explain(r):
    """Append a plain-English gloss to a raw PJLink response."""
    code = r.rsplit("=", 1)[-1].strip().upper() if "=" in r else ""
    return f"{r}  ({ERRORS[code]})" if code in ERRORS else r

def _readline(s, limit=512):
    """One CR-terminated line. A single recv() can return a partial line, which
    for the greeting means a truncated auth seed and a wrong MD5 digest."""
    buf=b""
    while b"\r" not in buf:
        if len(buf)>=limit: break
        chunk=s.recv(limit-len(buf))
        if not chunk: break
        buf+=chunk
    return buf.split(b"\r")[0].decode(errors="replace").strip()

def send(ip, cmd, pw=PASSWORD, retries=2):
    last=None
    for _ in range(retries+1):
        s=None
        try:
            s=socket.socket(); s.settimeout(5); s.connect((ip,4352))
            hello=_readline(s).split(" ")
            if len(hello)>=3 and hello[1]=="1":
                msg=hashlib.md5((hello[2]+pw).encode()).hexdigest()+cmd+"\r"
            else:
                msg=cmd+"\r"
            s.sendall(msg.encode())
            return _readline(s)
        except Exception as e:
            last=e; time.sleep(1.5)
        finally:
            if s is not None:
                try: s.close()
                except Exception: pass
    return f"ERR {last}"

def targets(which):
    if which=="both": return [("left",PROJECTORS["left"]),("right",PROJECTORS["right"])]
    if which in PROJECTORS: return [(which,PROJECTORS[which])]
    return None

def cmd_status():
    for name,ip in [("left",PROJECTORS["left"]),("right",PROJECTORS["right"])]:
        pwr=send(ip,"%1POWR ?"); time.sleep(1.2)
        inp=send(ip,"%1INPT ?"); time.sleep(1.2)
        lamp=send(ip,"%1LAMP ?"); time.sleep(1.2)
        erst=send(ip,"%1ERST ?"); time.sleep(1.2)
        pv=POWER.get(pwr.split("=")[-1],pwr)
        iv=INPUT_NAMES.get(inp.split("=")[-1],inp)
        lh=lamp.split("=")[-1].split(" ")[0] if "=" in lamp else lamp
        er="none" if erst.endswith("000000") else erst
        print(f"{name:5} {ip}: power={pv} input={iv} lamp={lh}h errors={er}")

def cmd_power(which,on):
    for name,ip in targets(which):
        st=send(ip,"%1POWR ?").rsplit("=",1)[-1].strip()
        if st in ("2","3"):
            # A power command during either transition is answered ERR3 and does
            # nothing (spec v1.04 s4.1). Say so rather than firing it blindly.
            print(f"{name} power {'ON' if on else 'OFF'}: skipped - projector is "
                  f"{POWER.get(st,st)}; it accepts power commands once that finishes")
            time.sleep(1.2); continue
        r=send(ip,"%1POWR "+("1" if on else "0"))
        print(f"{name} power {'ON' if on else 'OFF'}: {explain(r)}"); time.sleep(1.2)

def cmd_input(which,code):
    for name,ip in targets(which):
        r=send(ip,"%1INPT "+code)
        print(f"{name} input {INPUT_NAMES.get(code,code)}: {explain(r)}"); time.sleep(1.2)

def cmd_mute(which,on):
    for name,ip in targets(which):
        r=send(ip,"%1AVMT "+("31" if on else "30"))
        print(f"{name} mute {'ON' if on else 'OFF'}: {explain(r)}"); time.sleep(1.2)

def usage(): print(__doc__); sys.exit(1)

def main():
    a=sys.argv[1:]
    if not a: usage()
    if a[0]=="status": return cmd_status()
    if a[0]=="on":  return cmd_power("both",True)
    if a[0]=="off":
        ans=input("Power OFF both projectors? [y/N] ").strip().lower()
        return cmd_power("both",False) if ans=="y" else print("cancelled")
    which=a[0]
    if which not in PROJECTORS and which!="both": usage()
    if len(a)<2: usage()
    act=a[1]
    if act=="input":
        if len(a)<3 or a[2].lower() not in INPUTS: 
            print("inputs:", ", ".join(sorted(set(INPUTS)))); sys.exit(1)
        return cmd_input(which,INPUTS[a[2].lower()])
    if act=="mute":
        return cmd_mute(which, len(a)>2 and a[2].lower() in ("on","1","true"))
    if act in ("on","off"):
        return cmd_power(which, act=="on")
    usage()

if __name__=="__main__": main()
