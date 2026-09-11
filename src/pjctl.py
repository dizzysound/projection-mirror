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

def send(ip, cmd, pw=PASSWORD, retries=2):
    last=None
    for _ in range(retries+1):
        try:
            s=socket.socket(); s.settimeout(5); s.connect((ip,4352))
            hello=s.recv(64).decode(errors="replace").strip().split(" ")
            if len(hello)>=3 and hello[1]=="1":
                msg=hashlib.md5((hello[2]+pw).encode()).hexdigest()+cmd+"\r"
            else:
                msg=cmd+"\r"
            s.sendall(msg.encode())
            r=s.recv(256).decode(errors="replace").strip()
            s.close()
            return r
        except Exception as e:
            last=e; time.sleep(1.5)
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
        r=send(ip,"%1POWR "+("1" if on else "0"))
        print(f"{name} power {'ON' if on else 'OFF'}: {r}"); time.sleep(1.2)

def cmd_input(which,code):
    for name,ip in targets(which):
        r=send(ip,"%1INPT "+code)
        print(f"{name} input {INPUT_NAMES.get(code,code)}: {r}"); time.sleep(1.2)

def cmd_mute(which,on):
    for name,ip in targets(which):
        r=send(ip,"%1AVMT "+("31" if on else "30"))
        print(f"{name} mute {'ON' if on else 'OFF'}: {r}"); time.sleep(1.2)

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
