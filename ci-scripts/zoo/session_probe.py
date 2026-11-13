"""Runs inside a webtop container (as abc, with /lsiopy/bin/python3), piped in by zoo.py.

    capture-env <process>   save that desktop process's environment to /config/.zoo/env.{sh,json}
    app-pid                 pid of the running app (also saved to /config/.zoo/app.pid)
    bus-state desktop|app   Secret Service facts on that process's session bus, as JSON
    lock-default            lock the default collection on the app's bus
    keys <tok>...           type over the Selkies websocket; <tab> <enter> <esc> are keys
"""

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

ZOO = "/config/.zoo"
APP_COMM = "rustconn"


def pids_by_comm(comm):
    me = os.getuid()
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat(f"/proc/{d}").st_uid != me:
                continue
            if open(f"/proc/{d}/comm").read().strip() == comm:
                out.append(int(d))
        except OSError:
            pass
    return sorted(out)


def environ(pid):
    raw = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    env = {}
    for kv in raw:
        if b"=" in kv:
            k, v = kv.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def capture_env(*procs):
    env = None
    for proc in procs:
        for pid in reversed(pids_by_comm(proc)):
            try:
                env = environ(pid)  # some are non-dumpable (debian plasmashell)
                break
            except OSError:
                continue
        if env:
            break
    if not env:
        sys.exit(f"no readable environment among {procs}")
    os.makedirs(ZOO, exist_ok=True)
    with open(f"{ZOO}/env.sh", "w") as f:
        for k, v in env.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and k not in ("PWD", "OLDPWD", "SHLVL", "_"):
                f.write(f"export {k}={shlex.quote(v)}\n")
    json.dump(env, open(f"{ZOO}/env.json", "w"), indent=1)
    print(json.dumps({k: env.get(k) for k in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY",
                                              "XDG_CURRENT_DESKTOP", "XDG_RUNTIME_DIR")}))


def app_pid():
    pids = pids_by_comm(APP_COMM)
    pid = str(pids[0]) if pids else ""
    os.makedirs(ZOO, exist_ok=True)
    open(f"{ZOO}/app.pid", "w").write(pid)
    print(pid)


def dbus_send_bin():
    app_root = None
    for d in os.listdir("/config/proot-apps") if os.path.isdir("/config/proot-apps") else []:
        if d.endswith("_" + APP_COMM):
            app_root = f"/config/proot-apps/{d}"
    if shutil.which("dbus-send"):
        return ["dbus-send"]
    if app_root:
        return ["/config/.local/bin/proot", "-R", app_root + "/", "dbus-send"]
    return None


def call(addr, dest, path, method, *args, timeout=8):
    base = dbus_send_bin()
    if not base:
        return None, "no dbus-send"
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addr)
    cmd = base + ["--session", "--print-reply", f"--reply-timeout={timeout * 1000}", f"--dest={dest}", path, method, *args]
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    return (p.stdout if p.returncode == 0 else None), p.stderr.strip()[-300:]


def bus_addr(which):
    if which == "desktop":
        env = json.load(open(f"{ZOO}/env.json"))
    else:
        pid = open(f"{ZOO}/app.pid").read().strip()
        env = environ(int(pid)) if pid else {}
    return env.get("DBUS_SESSION_BUS_ADDRESS")


def bus_state(which):
    st = {"which": which}
    addr = bus_addr(which)
    st["bus"] = addr
    if not addr:
        print(json.dumps(st))
        return
    D = ("org.freedesktop.DBus", "/org/freedesktop/DBus")
    out, err = call(addr, *D, "org.freedesktop.DBus.NameHasOwner", "string:org.freedesktop.secrets")
    st["secrets_owned"] = bool(out and "true" in out)
    if out is None:
        st["error"] = err
    out, _ = call(addr, *D, "org.freedesktop.DBus.ListActivatableNames")
    st["secrets_activatable"] = bool(out and "org.freedesktop.secrets" in out)
    out, _ = call(addr, *D, "org.freedesktop.DBus.ListNames")
    st["names"] = sorted(set(re.findall(r'string "([a-zA-Z][^"]*)"', out or "")))
    if st["secrets_owned"]:
        out, _ = call(addr, *D, "org.freedesktop.DBus.GetConnectionUnixProcessID", "string:org.freedesktop.secrets")
        m = re.search(r"uint32 (\d+)", out or "")
        if m:
            pid = m.group(1)
            try:
                st["secrets_owner"] = open(f"/proc/{pid}/comm").read().strip()
            except OSError:
                st["secrets_owner"] = f"pid {pid}"
        S = ("org.freedesktop.secrets", "/org/freedesktop/secrets")
        out, _ = call(addr, *S, "org.freedesktop.Secret.Service.ReadAlias", "string:default")
        m = re.search(r'object path "([^"]+)"', out or "")
        default = m.group(1) if m else None
        st["default"] = default
        st["default_exists"] = bool(default and default != "/")
        if st["default_exists"]:
            out, _ = call(addr, "org.freedesktop.secrets", default, "org.freedesktop.DBus.Properties.Get",
                          "string:org.freedesktop.Secret.Collection", "string:Locked")
            st["default_locked"] = bool(out and "true" in out)
        out, _ = call(addr, *S, "org.freedesktop.DBus.Properties.Get",
                      "string:org.freedesktop.Secret.Service", "string:Collections")
        st["collections"] = re.findall(r'object path "([^"]+)"', out or "")
    else:
        st["secrets_owner"] = None
    print(json.dumps(st))


def lock_default():
    addr = bus_addr("app")
    if not addr:
        print("no app bus")
        return
    S = ("org.freedesktop.secrets", "/org/freedesktop/secrets")
    out, err = call(addr, *S, "org.freedesktop.Secret.Service.ReadAlias", "string:default")
    m = re.search(r'object path "([^"]+)"', out or "")
    if not m or m.group(1) == "/":
        print(f"no default collection ({err})")
        return
    out, err = call(addr, *S, "org.freedesktop.Secret.Service.Lock", f"array:objpath:{m.group(1)}")
    print(f"lock {m.group(1)}: {'ok' if out is not None else err}")


def x11():
    """Older Selkies (el images: X11, no screenshot API, websocket input not
    taken): grab and type through X instead. zoo.py sets ZOO_X11."""
    return os.environ.get("ZOO_X11") == "1"


def png():
    if x11():
        import io
        from PIL import ImageGrab
        buf = io.BytesIO()
        ImageGrab.grab(xdisplay=os.environ.get("DISPLAY", ":1")).save(buf, "PNG")
        return buf.getvalue()
    import urllib.request
    return urllib.request.urlopen("http://localhost:8082/api/screenshot", timeout=20).read()


def shot(name):
    """Save a screenshot to /config/.zoo/<name>.png (- writes it to stdout)."""
    data = png()
    if name == "-":
        sys.stdout.buffer.write(data)
        return
    os.makedirs(ZOO, exist_ok=True)
    open(f"{ZOO}/{name}.png", "wb").write(data)
    print(len(data))


def dialog_box(before, after):
    """Bounding box of what changed between two screenshots, ignoring the
    top and bottom panels (clocks tick). Prints JSON {x, y, w, h, sw, sh}."""
    from PIL import Image, ImageChops
    a = Image.open(f"{ZOO}/{before}.png").convert("RGB")
    b = Image.open(f"{ZOO}/{after}.png").convert("RGB")
    sw, sh = a.size
    # Dialogs open centered; the margins hold panels, clocks and desktop icons
    # (which light up when the launcher is clicked).
    left, right = int(sw * 0.15), int(sw * 0.85)
    top, bottom = int(sh * 0.10), int(sh * 0.90)
    diff = ImageChops.difference(a.crop((left, top, right, bottom)), b.crop((left, top, right, bottom)))
    # Ignore tiny noise (antialiasing, cursor blink) by thresholding.
    mask = diff.convert("L").point(lambda v: 255 if v > 24 else 0)
    box = mask.getbbox()
    if not box:
        print(json.dumps({"sw": sw, "sh": sh}))
        return
    x0, y0, x1, y1 = box
    print(json.dumps({"x": x0 + left, "y": y0 + top, "w": x1 - x0, "h": y1 - y0, "sw": sw, "sh": sh}))


KEYS = {"tab": 0xFF09, "enter": 0xFF0D, "esc": 0xFF1B, "backspace": 0xFF08, "space": 0x20,
        "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53}
MODS = {"shift": 0xFFE1, "ctrl": 0xFFE3, "alt": 0xFFE9}


def parse(tokens):
    """Text is typed as-is; <tab>, <alt-n>, <shift-tab>, <ctrl-a>, <sleep> are keys/combos."""
    seq = []
    for t in tokens:
        m = re.fullmatch(r"<([a-z0-9-]+)>", t)
        if not m:
            seq.extend([(ord(c),)] for c in t)
            continue
        if m.group(1) == "sleep":
            seq.append(["sleep"])
            continue
        c = re.fullmatch(r"click-(\d+)-(\d+)", m.group(1))
        if c:
            seq.append(["click", int(c.group(1)), int(c.group(2))])
            continue
        *mods, key = m.group(1).split("-")
        sym = KEYS.get(key, ord(key) if len(key) == 1 else None)
        if sym is None:
            sys.exit(f"unknown key {t}")
        seq.append([(MODS[x],) for x in mods] + [(sym,)])
    return seq


XDO_KEYS = {"tab": "Tab", "enter": "Return", "esc": "Escape", "backspace": "BackSpace", "space": "space",
            "up": "Up", "down": "Down", "left": "Left", "right": "Right"}


def keys_xdotool(tokens):
    import time
    for t in tokens:
        m = re.fullmatch(r"<([a-z0-9-]+)>", t)
        if not m:
            subprocess.run(["xdotool", "type", "--delay", "40", t], check=True)
        elif m.group(1) == "sleep":
            time.sleep(1.5)
        elif re.fullmatch(r"click-(\d+)-(\d+)", m.group(1)):
            _, x, y = m.group(1).split("-")
            subprocess.run(["xdotool", "mousemove", x, y, "click", "1"], check=True)
        else:
            *mods, key = m.group(1).split("-")
            subprocess.run(["xdotool", "key", "+".join(mods + [XDO_KEYS.get(key, key)])], check=True)
        time.sleep(0.1)
    time.sleep(0.5)
    print(f"typed {len(tokens)} tokens (xdotool)")


async def keys(tokens):
    if x11():
        return keys_xdotool(tokens)
    import websockets

    seq = parse(tokens)
    async with websockets.connect("ws://localhost:8082/api/websockets", max_size=None) as ws:
        try:
            while True:
                m = await asyncio.wait_for(ws.recv(), 1.5)
                if isinstance(m, str) and m.startswith('{"type": "server_settings"'):
                    break
        except asyncio.TimeoutError:
            pass
        for combo in seq:
            if combo == ["sleep"]:
                await asyncio.sleep(1.5)
                continue
            if combo[0] == "click":
                _, x, y = combo
                for mask in (0, 1, 0):
                    await ws.send(f"m,{x},{y},{mask},0")
                    await asyncio.sleep(0.08)
                continue
            for (s,) in combo:
                await ws.send(f"kd,{s}")
                await asyncio.sleep(0.03)
            for (s,) in reversed(combo):
                await ws.send(f"ku,{s}")
                await asyncio.sleep(0.03)
            await asyncio.sleep(0.06)
        await asyncio.sleep(0.5)
    print(f"typed {len(seq)} keys")


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "capture-env":
        capture_env(*args)
    elif cmd == "app-pid":
        app_pid()
    elif cmd == "bus-state":
        bus_state(args[0])
    elif cmd == "lock-default":
        lock_default()
    elif cmd == "shot":
        shot(args[0])
    elif cmd == "dialog-box":
        dialog_box(args[0], args[1])
    elif cmd == "keys":
        asyncio.run(keys(args))
    else:
        sys.exit(f"unknown {cmd}")


main()
