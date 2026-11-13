#!/usr/bin/env python3
"""Session zoo: run a proot-app across every SealSkin desktop x GPU scenario.

Launches webtop sessions the way SealSkin's docker provider does (Wayland
mode, same env, same GPU wiring, fresh /config, the app served from a
read-only local catalog through PA_REPO_FOLDER), installs the app with
proot-apps, launches it with the desktop's own environment (as a click on
its icon would), and runs the RustConn keyring checks headlessly. Dialogs
are answered by typing over the Selkies input websocket, like a user in the
browser would; screenshots come from Selkies' /api/screenshot.

    zoo.py prepare [--hosts ...] [--app-only]   build + publish app, pull images
    zoo.py run [--only REGEX] [--hosts ...]     run scenarios, 3 at a time per host
    zoo.py clean                                remove zoo containers and homes

Results land in zoo-results/<run id>/: one folder per scenario with
result.json, screenshots and logs, plus summary.md / summary.json.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
PROBE = (HERE / "session_probe.py").read_text()

APP = "rustconn"
REGISTRY = "ghcr.io/linuxserver/proot-apps"
APP_FOLDER = f"{REGISTRY.replace('/', '_')}_{APP}"
ZOO = "/root/pa-zoo"
KEYRING_PW = "zoopass2026"
SECRET = "zoo-secret-4711"

HOSTS = {
    "pve02": {"ssh": "root@pve02-dockerhost.internal", "gpus": ["intel", "none"]},
    "pve03": {"ssh": "root@pve03-dockerhost.internal", "gpus": ["nvidia"]},
}
GPU_NODES = {"intel": "/dev/dri/renderD128", "nvidia": "/dev/dri/renderD128"}
CONCURRENCY = 3

# The SealSkin Apps catalog: no el-kde, alpine-xfce is "latest".
IMAGES = {
    ("alpine", "xfce"): "latest",
    **{(d, de): f"{d}-{de}"
       for d in ("alpine", "arch", "debian", "ubuntu", "el", "fedora")
       for de in ("xfce", "kde", "i3", "mate")
       if (d, de) not in {("alpine", "xfce"), ("el", "kde")}},
}
# A process the desktop itself started: its environment is what a click on a
# launcher hands to the app (not the compositor's own, which points at the
# outer Selkies compositor). First match wins.
DE_PROCESS = {"kde": ["plasmashell", "kded6", "xdg-desktop-portal-kde", "kactivitymanagerd"], "xfce": ["xfce4-panel", "xfdesktop"],
              "mate": ["mate-panel", "caja"], "i3": ["swaybar", "i3bar"]}

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-zoo-%C", "-o", "ControlPersist=15m"]

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


class Remote:
    def __init__(self, name: str):
        self.name = name
        self.ssh = HOSTS[name]["ssh"]

    def run(self, script: str, timeout: float = 120, check: bool = False,
            stdin: bytes | None = None, binary: bool = False) -> subprocess.CompletedProcess:
        """Run a bash script on the host (script travels as an argument, stdin stays free)."""
        cmd = ["ssh", *SSH_OPTS, self.ssh, "bash -c " + shlex.quote(script)]
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            p = subprocess.CompletedProcess(cmd, 124, exc.stdout or b"", (exc.stderr or b"") + b"\n[timeout]")
        if not binary:
            p.stdout = p.stdout.decode(errors="replace")
            p.stderr = p.stderr.decode(errors="replace")
        if check and p.returncode != 0:
            raise RuntimeError(f"[{self.name}] rc={p.returncode}: {p.stderr[-2000:]}")
        return p


class NoDesktop(RuntimeError):
    """The webtop's own desktop did not start: nothing to test the app in."""


class Session:
    """One webtop container on one host."""

    def __init__(self, remote: Remote, sid: str, image: str, gpu: str, de: str, outdir: Path):
        self.r, self.sid, self.image, self.gpu, self.de, self.out = remote, sid, image, gpu, de, outdir
        self.ctr = f"zoo-{sid}"
        self.home = f"{ZOO}/homes/{sid}"
        self.shots = 0
        self.no_screenshots = False

    # -- plumbing -----------------------------------------------------------
    def sh(self, script: str, user: str = "abc", timeout: float = 120, stdin: bytes | None = None,
           env: bool = True) -> subprocess.CompletedProcess:
        """Run bash inside the container; with env, in the desktop's environment."""
        pre = "set -a; [ -f /config/.zoo/env.sh ] && . /config/.zoo/env.sh; set +a\n" if env else ""
        inner = shlex.quote(pre + script)
        return self.r.run(f"docker exec -i -u {user} {self.ctr} bash -c {inner}", timeout=timeout, stdin=stdin)

    def probe(self, *args: str, timeout: float = 60, binary: bool = False) -> subprocess.CompletedProcess:
        a = " ".join(shlex.quote(x) for x in args)
        x = "-e ZOO_X11=1 -e DISPLAY=:1 " if self.no_screenshots else ""
        return self.r.run(f"docker exec -i -u abc {x}{self.ctr} /lsiopy/bin/python3 - {a}",
                          timeout=timeout, stdin=PROBE.encode(), binary=binary)

    def screenshot(self, label: str) -> str | None:
        if self.no_screenshots:
            p = self.probe("shot", "-", timeout=40, binary=True)
        else:
            p = self.r.run(f"docker exec {self.ctr} curl -sf -m 20 http://localhost:8082/api/screenshot",
                           timeout=40, binary=True)
        if p.returncode != 0 or not p.stdout.startswith(b"\x89PNG"):
            return None
        self.shots += 1
        name = f"{self.shots:02d}-{label}.png"
        (self.out / name).write_bytes(p.stdout)
        return name

    def screen_size(self) -> tuple[int, int]:
        p = self.r.run(f"docker exec {self.ctr} sh -c 'curl -sf -m 20 http://localhost:8082/api/screenshot | head -c 24'",
                       timeout=40, binary=True)
        b = p.stdout
        if len(b) >= 24 and b.startswith(b"\x89PNG"):
            return int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
        return 1024, 768

    def keys(self, *tokens: str) -> bool:
        """Type over Selkies: text tokens are typed, <tab>/<enter>/<esc> are keys."""
        p = self.probe("keys", *tokens)
        return p.returncode == 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        gpu_args = ""
        if self.gpu == "intel":
            node = GPU_NODES["intel"]
            gpu_args = f"--device {node}:{node} -e DRI_NODE={node} -e DRINODE={node}"
        elif self.gpu == "nvidia":
            node = GPU_NODES["nvidia"]
            gpu_args = ("--runtime nvidia "
                        "--gpus 'device=0,\"capabilities=compute,video,graphics,utility,gpu,display\"' "
                        f"--device /dev/nvidia-modeset:/dev/nvidia-modeset "
                        f"-e DRI_NODE={node} -e DRINODE={node}")
        script = f"""
set -e
docker rm -f {self.ctr} >/dev/null 2>&1 || true
rm -rf {self.home}; mkdir -p {self.home}; chown 1000:1000 {self.home}
docker run -d --name {self.ctr} --label zoo=1 --shm-size 1g \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC -e PIXELFLUX_WAYLAND=true \
  -e NVIDIA_DRIVER_CAPABILITIES=all -e DISABLE_DRI3=false -e __GL_SYNC_TO_VBLANK=0 \
  -e PA_REPO_FOLDER=/mnt/proot-apps -e PA_DEFAULT_REPO={REGISTRY} \
  -v {self.home}:/config -v {ZOO}/repo:/mnt/proot-apps:ro \
  {gpu_args} lscr.io/linuxserver/webtop:{self.image}
"""
        self.r.run(script, timeout=180, check=True)

    def wait_ready(self, timeout: float = 300) -> dict:
        """Desktop process up, proot-apps installed, Selkies screenshot answering."""
        procs = "|".join(DE_PROCESS[self.de])
        deadline = time.time() + timeout
        state = {}
        while time.time() < deadline:
            p = self.sh(f"""
pgrep -u abc -x '{procs}' >/dev/null && echo de=1 || echo de=0
[ -x /config/.local/bin/proot-apps ] && echo pa=1 || echo pa=0
echo shot=$(curl -s -m 10 -o /dev/null -w '%{{http_code}}' http://localhost:8082/api/screenshot)
""", env=False, timeout=60)
            state = dict(l.split("=", 1) for l in p.stdout.split() if "=" in l)
            if state.get("de") == "1" and state.get("pa") == "1":
                # 200: Selkies screenshots work. 426/404: an older Selkies (el
                # images) without the screenshot API; input still works.
                if state.get("shot") in ("200", "426", "404"):
                    self.no_screenshots = state.get("shot") != "200"
                    time.sleep(8)  # let the desktop settle (wallet daemons, panels)
                    return state
            if self.r.run(f"docker inspect -f '{{{{.State.Running}}}}' {self.ctr}").stdout.strip() != "true":
                raise RuntimeError("container exited")
            time.sleep(5)
        if state.get("de") != "1":
            raise NoDesktop(f"desktop never came up in {timeout}s: {state}")
        raise RuntimeError(f"not ready after {timeout}s: {state}")

    def capture_env(self) -> None:
        p = self.probe("capture-env", *DE_PROCESS[self.de])
        if p.returncode != 0:
            raise RuntimeError(f"capture-env: {p.stderr[-1500:]}")

    def logs(self) -> None:
        p = self.r.run(f"docker logs --tail 400 {self.ctr} 2>&1", timeout=60)
        (self.out / "container.log").write_text(p.stdout)
        p = self.sh("cat /config/.zoo/gui-*.log 2>/dev/null | tr -d '\\000' | grep -a -v Fontconfig | tail -n 300", env=False)
        (self.out / "gui.log").write_text(p.stdout)

    def stop(self) -> None:
        self.r.run(f"docker rm -f {self.ctr} >/dev/null 2>&1; rm -rf {self.home}", timeout=120)


# -- the RustConn scenario --------------------------------------------------

def bus_state(s: Session, which: str) -> dict:
    p = s.probe("bus-state", which)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": (p.stdout + p.stderr)[-800:]}


def app_pid(s: Session) -> str:
    return s.probe("app-pid").stdout.strip()


def start_gui(s: Session, n: int) -> None:
    """Click the launcher: proot-apps run with the desktop's environment."""
    s.sh(f"mkdir -p /config/.zoo; rm -f /config/.zoo/app.pid; RUSTCONN_DEBUG=1 setsid nohup /config/.local/bin/proot-apps run {APP} "
         f"> /config/.zoo/gui-{n}.log 2>&1 < /dev/null &", timeout=30)


def wait_gui(s: Session, secs: float) -> str:
    deadline = time.time() + secs
    while time.time() < deadline:
        pid = app_pid(s)
        if pid:
            return pid
        time.sleep(1.5)
    return ""


def expected_dialog(desk: dict, launch: int) -> tuple[str, str]:
    """(keyring, dialog) the entrypoint should raise before RustConn starts."""
    if desk.get("secrets_owned") or desk.get("secrets_activatable"):
        owner = desk.get("secrets_owner") or "gnome-keyring-d"
        kind = "create" if not desk.get("default_exists") else (
            "unlock" if desk.get("default_locked") else "none")
        return owner, kind
    return "own gnome-keyring", "create" if launch == 1 else "unlock"


def stop_gui(s: Session) -> None:
    # By name: the command line under proot differs between hosts (el shows
    # a bare "rustconn"), the process name does not.
    s.sh("pkill -u abc -x rustconn; for i in $(seq 10); do pgrep -u abc -x rustconn >/dev/null || break; sleep 1; done; "
         "pkill -9 -u abc -x rustconn; true", env=False, timeout=30)
    time.sleep(3)


def cli(s: Session, args: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """rustconn-cli inside the app's rootfs, on the bus the running GUI uses."""
    pipe = f"printf %s {shlex.quote(stdin)} | " if stdin is not None else ""
    return s.sh(f"""
R=/config/proot-apps/{APP_FOLDER}
A=$(tr '\\0' '\\n' < /proc/$(cat /config/.zoo/app.pid)/environ | grep ^DBUS_SESSION_BUS_ADDRESS= || true)
[ -n "$A" ] && export "$A"
{pipe}timeout {timeout} /config/.local/bin/proot -R $R/ rustconn-cli -c /config/.zoo/cfg {args}
""", timeout=timeout + 30)


def keyring_ready(st: dict) -> bool:
    return bool(st.get("default_exists")) and not st.get("default_locked")


def dialog_box(s: Session) -> dict:
    """Where the dialog is: what changed since the pre-launch screenshot."""
    s.probe("shot", "dlg")
    p = s.probe("dialog-box", "pre", "dlg")
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def dismiss_i3_wizard(s: Session, res: dict, waits: int) -> None:
    """Press Escape on i3's first-run wizard until it is really gone: while it
    runs it holds the keyboard grab and keyring dialogs cannot take input."""
    def running() -> bool:
        return s.sh("pgrep -u abc -x i3-config-wizar >/dev/null", env=False, timeout=30).returncode == 0
    for _ in range(waits):
        if running():
            break
        if waits > 1:
            time.sleep(1)
    else:
        return
    for attempt in range(5):
        time.sleep(1)
        s.keys("<esc>")
        for _ in range(5):
            if not running():
                res["i3_wizard"] = f"dismissed ({attempt + 1} esc)"
                return
            time.sleep(1)
    res["i3_wizard"] = "still running"


def wait_dialog(s: Session, secs: float) -> None:
    """Wait until something dialog-sized appeared since the pre-launch
    screenshot, then give it a moment to take keyboard focus."""
    deadline = time.time() + secs
    while time.time() < deadline:
        box = dialog_box(s)
        if box.get("w", 0) > 150 and box.get("h", 0) > 80:
            break
        time.sleep(1)
    time.sleep(1.5)


def answer_keyring(s: Session, owner: str, kind: str, label: str) -> str:
    """Answer the keyring dialog as a user would.

    KWallet (ksecretd) creates a wallet either through a wizard (arch: pick
    "Classic, blowfish" since GPG needs a key nobody has, Finish, then
    password + verify) or straight with password + verify (alpine). The
    wizard is the wide one. gnome-keyring (gcr) asks for password + confirm.
    Unlock dialogs ask for the password.
    """
    s.screenshot(f"{label}-{kind}-dialog")
    how = ""
    if owner == "ksecretd" and kind == "create":
        box = dialog_box(s)
        wide = box.get("w", 0) > 0.44 * box.get("sw", 1024)  # wizard ~500px, password dialog ~400px at 1024
        if wide:
            # The wizard opens centered at a fixed size (arch, fedora): the
            # "Classic" radio sits 130px left of and 47px above screen center.
            # (The diff box itself picks up shadows on light wallpapers.)
            x = box.get("sw", 1024) // 2 - 130
            y = box.get("sh", 768) // 2 - 47
            s.keys(f"<click-{x}-{y}>", "<sleep>", "<enter>", "<sleep>")
            how = f"wizard {box['w']}x{box['h']}"
        else:
            how = f"password dialog {box.get('w')}x{box.get('h')}"
        s.keys(KEYRING_PW, "<tab>", KEYRING_PW, "<enter>")
    elif kind == "create":
        s.keys(KEYRING_PW, "<tab>", KEYRING_PW, "<enter>")
    else:
        s.keys(KEYRING_PW, "<enter>")
    return how


def launch(s: Session, res: dict, n: int) -> None:
    """Launch, answer the dialog the entrypoint raises first, wait for the GUI."""
    desk = bus_state(s, "desktop")
    owner, kind = expected_dialog(desk, n)
    # The app may not be on the desktop's bus (el: private bus). On relaunch,
    # what launch 1 left on the app's own bus decides: its keyring exists.
    if n == 2 and kind == "create" and (res.get("app_bus_1") or {}).get("default_exists"):
        kind = "unlock"
    res[f"expected{n}"] = f"{owner}: {kind}"
    if res["de"] == "i3":
        dismiss_i3_wizard(s, res, 1)
    s.probe("shot", "pre")
    start_gui(s, n)
    pid = wait_gui(s, 12)
    if pid:
        res[f"dialog{n}"] = "none shown" if kind == "none" else f"none shown (expected {kind})"
    else:
        # The entrypoint may have passed over the desktop's keyring (unreadable,
        # e.g. ubuntu-kde): whose dialog is up is what counts.
        if s.sh("pgrep -u abc -x gcr-prompter >/dev/null", env=False, timeout=30).returncode == 0:
            if owner == "ksecretd":
                owner, kind = "own gnome-keyring", ("create" if n == 1 else "unlock")
                res[f"expected{n}"] += f" -> {owner}: {kind}"
        wait_dialog(s, 25)
        how = answer_keyring(s, owner, kind, f"launch{n}")
        pid = wait_gui(s, 25)
        if not pid and kind != "none":
            # Keystrokes can land before the dialog takes input: answer again.
            how = (how + " " if how else "") + "retried"
            s.screenshot(f"launch{n}-retry")
            if kind == "create" and owner != "ksecretd":
                s.keys("<ctrl-a>", KEYRING_PW, "<tab>", "<ctrl-a>", KEYRING_PW, "<enter>")
            elif kind != "create":
                s.keys("<ctrl-a>", KEYRING_PW, "<enter>")
            pid = wait_gui(s, 30)
        res[f"dialog{n}"] = (f"{kind} answered" if pid else f"{kind} answered, app never started") + (
            f" ({how})" if how else "")
    time.sleep(6)
    pid = app_pid(s)
    res[f"launch{n}"] = {"alive": bool(pid), "pid": pid}
    s.screenshot(f"launch{n}")
    if not pid:
        raise RuntimeError(f"GUI not running after launch {n}")
    st = bus_state(s, "app")
    res[f"app_bus_{n}"] = st
    res[f"ready{n}"] = keyring_ready(st)


def lookup(s: Session) -> subprocess.CompletedProcess:
    """Read the password back the way RustConn's GUI does: secret-tool."""
    return s.sh(f"""
R=/config/proot-apps/{APP_FOLDER}
A=$(tr '\\0' '\\n' < /proc/$(cat /config/.zoo/app.pid)/environ | grep ^DBUS_SESSION_BUS_ADDRESS= || true)
[ -n "$A" ] && export "$A"
timeout 30 /config/.local/bin/proot -R $R/ secret-tool lookup application rustconn \
  connection_id 'RustConn/zoo (ssh)' key password
""", timeout=60)


def rustconn_scenario(s: Session, res: dict) -> None:
    res["desktop_bus"] = bus_state(s, "desktop")
    s.screenshot("desktop")

    # Install through the local catalog, like SealSkin's installer.
    p = s.sh(f"/config/.local/bin/proot-apps install {APP}", timeout=600)
    res["install"] = p.returncode == 0
    (s.out / "install.log").write_text(p.stdout + p.stderr)
    if not res["install"]:
        raise RuntimeError("install failed")

    # 1. First launch from the desktop: the keyring gets created.
    launch(s, res, 1)

    # 2. Store through RustConn's own secret code (oo7), read back via secret-tool.
    cli(s, "add -n zoo -H 127.0.0.1 -u zoo", timeout=30)
    p = cli(s, "secret set zoo --password-stdin -b libsecret", stdin=SECRET, timeout=60)
    res["store_rc"] = p.returncode
    res["store_out"] = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout + p.stderr)[-600:].strip()
    p = lookup(s)
    res["lookup1_ok"] = p.stdout.strip() == SECRET
    res["lookup1_out"] = (p.stdout + p.stderr)[-300:].strip()
    s.screenshot("stored")

    # 3. Relaunch with the keyring locked (next login / daemon restart).
    lock = s.probe("lock-default")
    res["lock"] = (lock.stdout + lock.stderr).strip()[-300:]
    stop_gui(s)
    launch(s, res, 2)
    p = lookup(s)
    res["lookup2_ok"] = p.stdout.strip() == SECRET
    res["lookup2_out"] = (p.stdout + p.stderr)[-300:].strip()
    s.screenshot("final")
    stop_gui(s)


def run_scenario(remote: Remote, distro: str, de: str, gpu: str, outroot: Path) -> dict:
    sid = f"{distro}-{de}-{gpu}"
    out = outroot / sid
    out.mkdir(parents=True, exist_ok=True)
    s = Session(remote, sid, IMAGES[(distro, de)], gpu, de, out)
    res = {"scenario": sid, "host": remote.name, "distro": distro, "de": de, "gpu": gpu,
           "image": s.image, "started": dt.datetime.now().isoformat(timespec="seconds")}
    t0 = time.time()
    try:
        log(f"[{remote.name}] {sid}: start")
        s.start()
        res["ready"] = s.wait_ready()
        res["screenshots"] = not s.no_screenshots
        # i3 on a fresh home opens its first-run config wizard, which grabs the
        # keyboard (el-i3). A user dismisses it first; so do we.
        # It can come up well after the desktop does (slow hosts), so on X11
        # i3 wait for it rather than look once.
        dismiss_i3_wizard(s, res, 20 if (res["de"] == "i3" and s.no_screenshots) else 1)
        s.capture_env()
        rustconn_scenario(s, res)
        res["status"] = verdict(res)
    except NoDesktop as exc:
        res["status"] = "NO-DESKTOP"
        res["error"] = f"{exc}"
        try:
            s.screenshot("no-desktop")
        except Exception:
            pass
    except Exception as exc:
        res["status"] = "ERROR"
        res["error"] = f"{exc}"
        res["trace"] = traceback.format_exc()[-3000:]
        try:
            s.screenshot("error")
        except Exception:
            pass
    finally:
        try:
            s.logs()
        except Exception:
            pass
        if not os.environ.get("ZOO_KEEP"):
            s.stop()
        res["seconds"] = round(time.time() - t0)
        (out / "result.json").write_text(json.dumps(res, indent=2))
        log(f"[{remote.name}] {sid}: {res['status']} ({res['seconds']}s)"
            + (f" {res.get('error', '')}" if res["status"] in ("ERROR", "NO-DESKTOP") else ""))
    return res


def verdict(res: dict) -> str:
    ok = (res.get("launch1", {}).get("alive") and res.get("launch2", {}).get("alive")
          and res.get("store_rc") == 0 and res.get("lookup1_ok") and res.get("lookup2_ok"))
    return "PASS" if ok else "FAIL"


# -- commands ---------------------------------------------------------------

def prepare(hosts: list[str], app_only: bool) -> None:
    src = REPO_ROOT / "apps" / APP
    tar = subprocess.run(["tar", "czf", "-", "--no-xattrs", "--exclude", "CLAUDE.md",
                          "--exclude", "package_versions.txt", "-C", str(src.parent), APP],
                         capture_output=True, check=True).stdout

    def one(h: str) -> None:
        r = Remote(h)
        log(f"[{h}] building {APP}")
        r.run(f"rm -rf {ZOO}/src && mkdir -p {ZOO}/src {ZOO}/homes && tar xzf - -C {ZOO}/src 2>/dev/null",
              stdin=tar, check=True)
        r.run(f"""
set -e
cd {ZOO}/src/{APP}
docker build -q --build-arg REPO=linuxserver/proot-apps -t pa-zoo/{APP}:test . >/dev/null
d={ZOO}/repo/{APP_FOLDER}; rm -rf $d; mkdir -p $d
cid=$(docker create pa-zoo/{APP}:test /entrypoint)
docker export $cid | gzip -1 > $d/app.tar.gz
docker rm $cid >/dev/null
echo "sha256:$(sha256sum $d/app.tar.gz | cut -d' ' -f1)" > $d/SHALAYER
chmod -R a+rX {ZOO}/repo
""", timeout=1800, check=True)
        log(f"[{h}] app published to {ZOO}/repo")
        if not app_only:
            imgs = " ".join(sorted({f"lscr.io/linuxserver/webtop:{t}" for t in IMAGES.values()}))
            log(f"[{h}] pulling {len(IMAGES)} webtop images")
            r.run(f"printf '%s\\n' {imgs} | xargs -P4 -n1 docker pull -q", timeout=7200, check=True)
            log(f"[{h}] images ready")

    with cf.ThreadPoolExecutor(len(hosts)) as ex:
        for f in [ex.submit(one, h) for h in hosts]:
            f.result()


def scenarios(hosts: list[str], only: str | None):
    for h in hosts:
        for gpu in HOSTS[h]["gpus"]:
            for (distro, de) in IMAGES:
                sid = f"{distro}-{de}-{gpu}"
                if only and not re.search(only, sid):
                    continue
                yield h, distro, de, gpu


def run(hosts: list[str], only: str | None, outroot: Path) -> None:
    outroot.mkdir(parents=True, exist_ok=True)
    todo = list(scenarios(hosts, only))
    log(f"{len(todo)} scenarios -> {outroot}")
    pools = {h: cf.ThreadPoolExecutor(CONCURRENCY, thread_name_prefix=h) for h in hosts}
    futs = [pools[h].submit(run_scenario, Remote(h), d, de, g, outroot) for h, d, de, g in todo]
    results = []
    for f in cf.as_completed(futs):
        results.append(f.result())
        summarize(results, outroot)
    for p in pools.values():
        p.shutdown()
    summarize(results, outroot)
    c = {k: sum(r["status"] == k for r in results) for k in ("PASS", "FAIL", "ERROR", "NO-DESKTOP")}
    log(f"done: {c}  summary: {outroot / 'summary.md'}")


def summarize(results: list[dict], outroot: Path) -> None:
    results = sorted(results, key=lambda r: (r["gpu"], r["distro"], r["de"]))
    (outroot / "summary.json").write_text(json.dumps(results, indent=2))
    rows = ["| scenario | status | desktop keyring | app keyring | launch 1 | store | read | launch 2 | read after unlock | notes |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    yn = lambda v: "✅" if v else ("❌" if v is not None else "–")
    for r in results:
        db = r.get("desktop_bus", {})
        ab = r.get("app_bus_1", {})
        desk = db.get("secrets_owner") or ("activatable" if db.get("secrets_activatable") else
                                           ("no bus" if not db.get("bus") else
                                            ("dead bus" if "Failed to connect" in db.get("error", "") else "none")))
        app_own = ab.get("secrets_owner") or "?"
        if ab.get("bus") and ab.get("bus") != db.get("bus"):
            app_own += " (private bus)"
        rows.append("| {} | {} | {} | {} | {} {} | {} | {} | {} {} | {} | {} |".format(
            r["scenario"], r["status"], desk, app_own,
            yn(r.get("launch1", {}).get("alive")), r.get("dialog1", ""),
            yn(r.get("store_rc") == 0 if "store_rc" in r else None), yn(r.get("lookup1_ok")),
            yn(r.get("launch2", {}).get("alive")), r.get("dialog2", ""),
            yn(r.get("lookup2_ok")),
            (r.get("error") or "").replace("|", "/")[:80]))
    (outroot / "summary.md").write_text("\n".join(rows) + "\n")


def clean(hosts: list[str]) -> None:
    for h in hosts:
        Remote(h).run(f"docker ps -aq --filter label=zoo=1 | xargs -r docker rm -f; rm -rf {ZOO}/homes/*",
                      timeout=300)
        log(f"[{h}] cleaned")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["prepare", "run", "clean"])
    ap.add_argument("--hosts", nargs="+", default=list(HOSTS))
    ap.add_argument("--only", help="regex over scenario ids like arch-kde-intel")
    ap.add_argument("--app-only", action="store_true", help="prepare: skip pulling webtop images")
    ap.add_argument("--out", default=str(REPO_ROOT / "zoo-results"))
    a = ap.parse_args()
    if a.cmd == "prepare":
        prepare(a.hosts, a.app_only)
    elif a.cmd == "run":
        run(a.hosts, a.only, Path(a.out) / dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    else:
        clean(a.hosts)


if __name__ == "__main__":
    main()
