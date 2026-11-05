#!/usr/bin/env python3
"""terminal-linux: a separate Linux desktop (QEMU/KVM) for computer-use agents, viewed from a terminal.

Port of terminal-mac. Nothing runs inside the guest: the host drives QEMU over QMP
(`screendump` for the screen, `input-send-event` / `send-key` for input). The host desktop is
never touched. Stdlib only.

  up [NAME] [--iso PATH] [--no-view] [--plan]   guided: download image, create, start, view
  create NAME [--iso PATH] [--cpus 4] [--memory 4] [--disk 32] [--width 1280] [--height 800]
              [--layout us|fr] [--offline]
  ls | status NAME | doctor
  start NAME [--window] | stop NAME [--force] [--timeout 60] | pause NAME | resume NAME
  view NAME          interactive terminal viewer (Kitty, Ghostty, WezTerm); Ctrl+] exits, Ctrl+O window
  show NAME | hide NAME   native window (VNC viewer)
  action NAME screenshot|click|move|drag|scroll|type|key ...
  mcp [NAME]         stdio MCP server
  skills get | skills install [--dir DIR]
"""
import argparse, base64, fcntl, hashlib, json, os, re, select, shutil, signal, socket, struct, subprocess, sys, \
    termios, time, tty, urllib.request

VERSION = "0.2.0"
STORE = os.environ.get("TERMINAL_LINUX_HOME") or os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "terminal-linux")
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, "skills", "terminal-linux", "SKILL.md")
ABS_MAX = 0x7FFF  # usb-tablet absolute axis range
IMAGE_INDEX = "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
IMAGE_PATTERN = r"debian-live-[\d.]+-amd64-xfce\.iso"


def vm_dir(name):
    if not name or "/" in name or name.startswith(".") or name == "images":
        raise SystemExit(f"invalid VM name: {name!r}")
    return os.path.join(STORE, name)


def load(name):
    try:
        with open(os.path.join(vm_dir(name), "machine.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"no VM named {name!r}; create it with: terminal-linux up {name}")


# ---------- QMP ----------

class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        try:
            self.sock.connect(path)
        except OSError as e:
            raise RuntimeError("VM is not running (cannot reach its QMP socket)") from e
        self.buf = b""
        self._recv()  # greeting
        self.cmd("qmp_capabilities")

    def _recv(self):
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QMP connection closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute, **arguments):
        msg = {"execute": execute}
        if arguments:
            msg["arguments"] = arguments
        self.sock.sendall(json.dumps(msg).encode() + b"\n")
        while True:
            reply = self._recv()
            if "event" in reply:
                continue
            if "error" in reply:
                raise RuntimeError(f"{execute}: {reply['error'].get('desc')}")
            return reply.get("return")

    def close(self):
        self.sock.close()


def qmp(name):
    return QMP(os.path.join(vm_dir(name), "qmp.sock"))


def state(name):
    """running / paused / stopped, without raising."""
    try:
        q = qmp(name)
    except RuntimeError:
        return "stopped"
    try:
        return "paused" if q.cmd("query-status")["status"] == "paused" else "running"
    finally:
        q.close()


class QGA:
    """Minimal qemu-guest-agent client. Raises OSError when no agent answers."""
    def __init__(self, name, timeout=0.5):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(os.path.join(vm_dir(name), "qga.sock"))
        self.buf = b""
        sync = int(time.time() * 1000) % 2**31
        while self.call("guest-sync", id=sync) != sync:  # drop replies left over from earlier clients
            pass

    def call(self, execute, **arguments):
        self.sock.sendall(json.dumps({"execute": execute, **({"arguments": arguments} if arguments else {})}).encode() + b"\n")
        while True:
            while b"\n" not in self.buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OSError("guest agent closed the connection")
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                reply = json.loads(line.strip(b"\xff") or b"{}")
            except ValueError:
                continue
            if "error" in reply:
                raise OSError(reply["error"].get("desc", "guest agent error"))
            if "return" in reply:
                return reply["return"]

    def read(self, path):
        h = self.call("guest-file-open", path=path)
        try:
            return base64.b64decode(self.call("guest-file-read", handle=h, count=65536)["buf-b64"]).decode()
        finally:
            self.call("guest-file-close", handle=h)


def _kv(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def guest_users(name):
    """Users of the active graphical session: [user], [] at a login screen, None when unknown.
    Reads systemd-logind's seat/session state through qemu-guest-agent (read-only, nothing executed).
    Only a definite answer is returned: callers may block actions on [] and must not guess."""
    try:
        g = QGA(name)
    except OSError:
        return None  # no agent in the guest
    try:
        try:
            seat = _kv(g.read("/run/systemd/seats/seat0"))
        except OSError:  # no logind: utmp is the only source, and an empty utmp proves nothing
            return sorted({u["user"] for u in g.call("guest-get-users")}) or None
        if not seat.get("ACTIVE"):
            return []
        session = _kv(g.read(f"/run/systemd/sessions/{seat['ACTIVE']}"))
        return [session["USER"]] if session.get("CLASS") == "user" else []  # "greeter" = login screen
    except (OSError, KeyError):
        return None
    finally:
        g.sock.close()


# ---------- screen ----------

def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("screendump did not produce a PNG (QEMU >= 7.1 required)")
    return struct.unpack(">II", data[16:24])


def screenshot(name, q=None, file="screen.png"):
    path = os.path.join(vm_dir(name), file)
    (q or qmp(name)).cmd("screendump", filename=path, format="png")  # QEMU's QMP socket takes one client at a time
    with open(path, "rb") as f:
        data = f.read()
    return data, png_size(data)


# ---------- keyboard layouts ----------
# Each character maps to a sequence of chords; a chord is a list of QEMU qcodes pressed together.
# qcodes name physical keys by their US position, so other layouts are tables over those keys.

def _us():
    m = {c: [[c]] for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
    m.update({c.upper(): [["shift", c]] for c in "abcdefghijklmnopqrstuvwxyz"})
    plain = dict(zip("-=[]\\;'`,./", "minus equal bracket_left bracket_right backslash semicolon apostrophe "
                                     "grave_accent comma dot slash".split()))
    m.update({c: [[k]] for c, k in plain.items()})
    m.update({c: [["shift", k]] for c, k in zip('!@#$%^&*()_+{}|:"~<>?', list("1234567890") + list(plain.values()))})
    return m


def _fr():
    """Linux xkb fr (AZERTY, basic variant)."""
    swap = {"a": "q", "q": "a", "z": "w", "w": "z", "m": "semicolon"}
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        k = swap.get(c, c)
        m[c], m[c.upper()] = [[k]], [["shift", k]]
    for c, k in zip("&é\"'(-è_çà", "1234567890"):
        m[c], m[k] = [[k]], [["shift", k]]
    pairs = {"minus": ")°", "equal": "=+", "bracket_right": "$£", "apostrophe": "ù%", "backslash": "*µ",
             "m": ",?", "comma": ";.", "dot": ":/", "slash": "!§", "less": "<>"}
    for k, (low, high) in pairs.items():
        m[low], m[high] = [[k]], [["shift", k]]
    m["²"] = [["grave_accent"]]
    for c, k in list(zip("~#{[|`\\^@", "234567890")) + [("]", "minus"), ("}", "equal"), ("€", "e")]:
        m[c] = [["alt_r", k]]
    for base, circ, diaer in zip("aeiou", "âêîôû", "äëïöü"):  # dead keys on the ^ key
        m[circ], m[diaer] = [["bracket_left"]] + m[base], [["shift", "bracket_left"]] + m[base]
    return m


LAYOUTS = {"us": _us(), "fr": _fr()}
COMMON = {" ": [["spc"]], "\n": [["ret"]], "\t": [["tab"]]}
ALIASES = {"return": "ret", "enter": "ret", "escape": "esc", "space": "spc", "cmd": "meta_l", "super": "meta_l",
           "win": "meta_l", "meta": "meta_l", "control": "ctrl", "option": "alt", "del": "delete",
           "pageup": "pgup", "pagedown": "pgdn", "altgr": "alt_r"}


def char_keys(ch, layout="us"):
    """Chord sequence typing one character. Characters missing from the layout use the
    Ctrl+Shift+U <hex> Unicode entry of GTK/Qt/IBus. shortcut: that fallback does not work in
    plain X11 or console apps; a per-layout table entry is the upgrade for frequent characters."""
    m = LAYOUTS[layout]
    if ch in COMMON:
        return COMMON[ch]
    if ch in m:
        return m[ch]
    if not ch.isprintable():
        raise ValueError(f"cannot type control character {ch!r}; use the key tool")
    return [["ctrl", "shift", "u"]] + [c for h in f"{ord(ch):x}" for c in m[h]] + [["spc"]]


def chord_keys(chord, layout="us"):
    keys = []
    for part in chord.split("+"):
        k = part.strip()
        if not k:
            raise ValueError(f"invalid key chord: {chord!r}")
        if len(k) == 1 and k.lower() in LAYOUTS[layout] and len(LAYOUTS[layout][k.lower()]) == 1:
            keys.append(LAYOUTS[layout][k.lower()][0][-1])  # physical key of that letter in the guest layout
        else:
            keys.append(ALIASES.get(k.lower(), k.lower()))
    return keys


# ---------- input ----------

def send_keys(q, keys):
    q.cmd("send-key", keys=[{"type": "qcode", "data": k} for k in keys])
    time.sleep(0.02)


def type_text(q, text, layout):
    seqs = [char_keys(c, layout) for c in text]  # validate everything before typing anything
    for seq in seqs:
        for chord in seq:
            send_keys(q, chord)


def to_abs(v, size):
    return round(max(0, min(v, size - 1)) * ABS_MAX / max(1, size - 1))


def move(q, x, y, size):
    q.cmd("input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": to_abs(x, size[0])}},
        {"type": "abs", "data": {"axis": "y", "value": to_abs(y, size[1])}}])


def button(q, btn, down):
    q.cmd("input-send-event", events=[{"type": "btn", "data": {"down": down, "button": btn}}])


def wheel(q, up, notches=1):
    for _ in range(notches):  # press+release in one batch: split batches are dropped by the guest
        q.cmd("input-send-event", events=[{"type": "btn", "data": {"down": d, "button": "wheel-up" if up else "wheel-down"}}
                                          for d in (True, False)])


def act(name, tool, a):
    """Shared by the CLI and MCP. Coordinates are pixels of the latest screenshot."""
    q = qmp(name)
    try:
        return _act(name, tool, a, q)
    finally:
        q.close()


def _act(name, tool, a, q):
    if tool == "screenshot":
        return screenshot(name, q)
    if tool == "status":
        _, (w, h) = screenshot(name, q)
        users = guest_users(name)
        return {"status": q.cmd("query-status")["status"], "width": w, "height": h,
                "users": users if users is not None else "unknown (guest agent not answering: not installed, or VM paused)"}
    if q.cmd("query-status")["status"] == "paused":
        raise RuntimeError("VM is paused; run: terminal-linux resume " + name)
    layout = load(name).get("layout", "us")
    if tool in ("click", "move", "drag", "scroll"):
        _, size = screenshot(name, q)  # current resolution, to scale into the tablet range
    if tool == "click":
        move(q, a["x"], a["y"], size)
        btn = a.get("button", "left")
        if btn not in ("left", "right", "middle"):
            raise ValueError("button must be left, right or middle")
        for _ in range(int(a.get("clicks", 1))):
            button(q, btn, True); button(q, btn, False); time.sleep(0.05)
    elif tool == "move":
        move(q, a["x"], a["y"], size)
    elif tool == "drag":
        move(q, a["x"], a["y"], size); button(q, "left", True)
        for i in range(1, 11):
            move(q, a["x"] + (a["x2"] - a["x"]) * i / 10, a["y"] + (a["y2"] - a["y"]) * i / 10, size)
            time.sleep(0.02)
        button(q, "left", False)
    elif tool == "scroll":
        if "x" in a and "y" in a:
            move(q, a["x"], a["y"], size)
        dy = int(a["dy"])  # negative scrolls down, like terminal-mac
        wheel(q, dy > 0, max(1, abs(dy) // 100))
    elif tool == "type":
        type_text(q, a["text"], layout)
    elif tool == "key":
        send_keys(q, chord_keys(a["key"], layout))
    else:
        raise ValueError(f"unknown action {tool!r}")
    return {"ok": True}


# ---------- VM lifecycle ----------

def create(name, iso, cpus=4, memory=4, disk=32, width=1280, height=800, layout="us", offline=False):
    d = vm_dir(name)
    if os.path.exists(os.path.join(d, "machine.json")):
        raise SystemExit(f"VM {name!r} already exists in {d}")
    if not os.path.isfile(iso):
        raise SystemExit(f"ISO not found: {iso}")
    if layout not in LAYOUTS:
        raise SystemExit(f"unknown layout {layout!r}; choose from {', '.join(LAYOUTS)}")
    if not (1 <= cpus <= os.cpu_count() and 1 <= memory <= 512 and 4 <= disk and 640 <= width <= 7680 and 480 <= height <= 4320):
        raise SystemExit("invalid size: check --cpus (<= host CPUs), --memory GiB, --disk GiB (>= 4), --width/--height")
    os.makedirs(d, mode=0o700, exist_ok=True)
    subprocess.run(["qemu-img", "create", "-f", "qcow2", os.path.join(d, "disk.qcow2"), f"{disk}G"],
                   check=True, stdout=subprocess.DEVNULL)
    with open(os.path.join(d, "machine.json"), "w") as f:
        json.dump({"iso": os.path.abspath(iso), "memory_mb": memory * 1024, "cpus": cpus, "width": width,
                   "height": height, "layout": layout, "offline": offline}, f)
    print(json.dumps({"created": name, "path": d}))


def start(name, window=False):
    d, m = vm_dir(name), load(name)
    sock = os.path.join(d, "qmp.sock")
    if os.path.exists(sock):
        if state(name) != "stopped":
            raise SystemExit(f"{name} is already running")
        os.unlink(sock)  # stale socket from a crashed VM
    cmd = ["qemu-system-x86_64", "-name", name, "-enable-kvm", "-cpu", "host", "-machine", "q35",
           "-m", str(m["memory_mb"]), "-smp", str(m["cpus"]),
           "-drive", f"file={os.path.join(d, 'disk.qcow2')},if=virtio",  # QEMU locks the image: one VM per disk
           "-cdrom", m["iso"], "-boot", "order=cd",
           "-device", f"virtio-vga,xres={m.get('width', 1280)},yres={m.get('height', 800)}",
           "-device", "qemu-xhci", "-device", "usb-tablet",
           "-nic", "none" if m.get("offline") else "user,model=virtio-net-pci",
           "-device", "virtio-serial-pci",
           "-chardev", f"socket,path={os.path.join(d, 'qga.sock')},server=on,wait=off,id=qga0",
           "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
           "-qmp", f"unix:{sock},server=on,wait=off",
           "-qmp", f"unix:{os.path.join(d, 'qmp-view.sock')},server=on,wait=off",  # the viewer's own monitor

           "-vnc", f"unix:{os.path.join(d, 'vnc.sock')}",
           "-pidfile", os.path.join(d, "qemu.pid"),
           "-display", "gtk" if window else "none"]
    log = open(os.path.join(d, "qemu.log"), "ab")
    subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        if os.path.exists(sock):
            print(json.dumps({"started": name})); return
        time.sleep(0.1)
    raise SystemExit(f"QEMU did not start; see {os.path.join(d, 'qemu.log')}")


def stop(name, force=False, timeout=60):
    with open(os.path.join(vm_dir(name), "qemu.pid")) as f:
        pid = int(f.read())
    if force:
        os.kill(pid, signal.SIGTERM)  # can lose guest work
        print(json.dumps({"stopped": name, "forced": True})); return
    q = qmp(name)
    q.cmd("system_powerdown")
    q.close()
    for _ in range(timeout * 2):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(json.dumps({"stopped": name})); return
        time.sleep(0.5)
    print(f"{name}: guest ignored the shutdown request for {timeout}s (no ACPI handling, or a confirmation "
          f"dialog is open); take a screenshot, or use stop --force", file=sys.stderr)
    sys.exit(1)


def pause(name, resume=False):
    q = qmp(name)
    q.cmd("cont" if resume else "stop")
    q.close()
    print(json.dumps({"name": name, "status": "running" if resume else "paused"}))


def ls():
    out = []
    if os.path.isdir(STORE):
        for n in sorted(os.listdir(STORE)):
            if os.path.isfile(os.path.join(STORE, n, "machine.json")):
                m = load(n)
                out.append({"name": n, "status": state(n), "cpus": m["cpus"], "memory_gb": m["memory_mb"] // 1024,
                            "display": f"{m.get('width', 1280)}x{m.get('height', 800)}", "layout": m.get("layout", "us")})
    print(json.dumps(out, indent=1))


# ---------- native window (VNC) ----------

VIEWERS = [("remote-viewer", lambda s: ["remote-viewer", f"vnc+unix://{s}"]),
           ("vncviewer", lambda s: ["vncviewer", s])]  # TigerVNC accepts a socket path


def show(name):
    d = vm_dir(name)
    if state(name) == "stopped":
        raise SystemExit(f"{name} is not running; run: terminal-linux start {name}")
    for exe, argv in VIEWERS:
        if shutil.which(exe):
            p = subprocess.Popen(argv(os.path.join(d, "vnc.sock")), start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(os.path.join(d, "viewer.pid"), "w") as f:
                f.write(str(p.pid))
            print(json.dumps({"shown": name, "viewer": exe})); return
    raise SystemExit("no VNC viewer found: install tigervnc-viewer (or virt-viewer)")


def hide(name):
    path = os.path.join(vm_dir(name), "viewer.pid")
    try:
        with open(path) as f:
            os.kill(int(f.read()), signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        pass
    if os.path.exists(path):
        os.unlink(path)
    print(json.dumps({"hidden": name}))  # the VM keeps running


# ---------- interactive terminal viewer ----------

CSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
            "P": "f1", "Q": "f2", "R": "f3", "S": "f4"}
TILDE_KEYS = {"2": "insert", "3": "delete", "5": "pgup", "6": "pgdn", "15": "f5", "17": "f6", "18": "f7",
              "19": "f8", "20": "f9", "21": "f10", "23": "f11", "24": "f12"}
_MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_CSI = re.compile(r"\x1b[\[O](\d*)(?:;(\d+))?([A-Za-z~])")


def parse_input(data):
    """Terminal bytes (decoded) -> events: ("text", s) ("key", chord) ("mouse", code, x, y, pressed) ("quit",) ("show",)."""
    ev, i = [], 0
    while i < len(data):
        c = data[i]
        if c == "\x1b":
            m = _MOUSE.match(data, i)
            if m:
                ev.append(("mouse", int(m[1]), int(m[2]), int(m[3]), m[4] == "M")); i = m.end(); continue
            m = _CSI.match(data, i)
            if m:
                num, mod, final = m.groups()
                name = TILDE_KEYS.get(num) if final == "~" else CSI_KEYS.get(final)
                if name:
                    bits = int(mod) - 1 if mod else 0
                    mods = [k for b, k in ((4, "ctrl"), (2, "alt"), (1, "shift")) if bits & b]
                    ev.append(("key", "+".join(mods + [name])))
                i = m.end(); continue
            if i + 1 < len(data) and data[i + 1].isprintable():
                ev.append(("key", "alt+" + data[i + 1])); i += 2; continue
            ev.append(("key", "esc")); i += 1; continue
        if c == "\x1d":
            ev.append(("quit",))
        elif c == "\x0f":
            ev.append(("show",))
        elif c in "\r\n":
            ev.append(("key", "ret"))
        elif c == "\t":
            ev.append(("key", "tab"))
        elif c in "\x7f\x08":
            ev.append(("key", "backspace"))
        elif ord(c) < 32:
            ev.append(("key", "ctrl+" + chr(ord(c) + 96)))
        elif ev and ev[-1][0] == "text":
            ev[-1] = ("text", ev[-1][1] + c)
        else:
            ev.append(("text", c))
        i += 1
    return ev


def kitty_supported():
    e = os.environ
    return "KITTY_WINDOW_ID" in e or e.get("TERM") == "xterm-kitty" or e.get("TERM_PROGRAM", "").lower() in ("ghostty", "wezterm")


def term_geometry():
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        (cols, rows), xp, yp = shutil.get_terminal_size(), 0, 0
    return cols, rows, (xp / cols if xp else 8.0), (yp / rows if yp else 16.0)


def fit(w, h, cols, rows, cw, ch):
    """Cells used to show a w x h image in cols x (rows-1) cells, keeping its aspect ratio."""
    scale = min(cols * cw / w, (rows - 1) * ch / h)
    return max(1, int(w * scale / cw)), max(1, int(h * scale / ch))


def view(name, fps=8):
    if not kitty_supported():
        print("This terminal has no Kitty graphics; opening the native window instead.", file=sys.stderr)
        return show(name)
    layout = load(name).get("layout", "us")
    # own QMP monitor and capture file, so agents keep working while someone watches
    q, out, fd = QMP(os.path.join(vm_dir(name), "qmp-view.sock")), sys.stdout, sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    # alt screen, hide cursor, report all mouse motion as SGR pixels
    out.write("\x1b[?1049h\x1b[?25l\x1b[?1003h\x1b[?1006h\x1b[?1016h")
    last, geom, held = None, None, set()
    try:
        while True:
            data, (w, h) = screenshot(name, q, "view.png")
            g = term_geometry()
            if data != last or g != geom:
                last, geom = data, g
                cols, rows, cw, ch = g
                cu, ru = fit(w, h, cols, rows, cw, ch)
                b64 = base64.standard_b64encode(data).decode()
                out.write("\x1b[H")
                for i in range(0, len(b64), 4096):
                    head = f"a=T,f=100,i=1,c={cu},r={ru},C=1,q=2," if i == 0 else ""
                    out.write(f"\x1b_G{head}m={1 if i + 4096 < len(b64) else 0};{b64[i:i + 4096]}\x1b\\")
                out.write(f"\x1b[{rows};1H\x1b[2K\x1b[2m {name} · {w}x{h} · Ctrl+] exit · Ctrl+O window\x1b[0m")
                out.flush()
            if not select.select([fd], [], [], 1 / fps)[0]:
                continue
            for e in parse_input(os.read(fd, 65536).decode("utf-8", "replace")):
                if e[0] == "quit":
                    return
                if e[0] == "show":
                    show(name)
                elif e[0] == "text":
                    type_text(q, "".join(c for c in e[1] if c.isprintable()), layout)
                elif e[0] == "key":
                    send_keys(q, chord_keys(e[1], layout))
                elif e[0] == "mouse":
                    code, px, py, pressed = e[1:]
                    x, y = px * w / (cu * cw), py * h / (ru * ch)
                    if x >= w or y >= h:
                        continue  # outside the image (status line or margin)
                    move(q, x, y, (w, h))
                    if code & 64:
                        wheel(q, code & 1 == 0)
                    elif not code & 32:
                        btn = ("left", "middle", "right")[code & 3] if code & 3 < 3 else None
                        if btn and pressed and btn not in held:
                            button(q, btn, True); held.add(btn)
                        elif btn and not pressed:
                            button(q, btn, False); held.discard(btn)
    finally:
        for btn in held:
            button(q, btn, False)
        q.close()
        out.write("\x1b_Ga=d,i=1\x1b\\\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?25h\x1b[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------- guided setup, diagnostics, skills ----------

def default_image(plan=False):
    """Latest Debian live XFCE ISO, cached and SHA-256 verified."""
    index = urllib.request.urlopen(IMAGE_INDEX, timeout=30).read().decode()
    iso = re.search(IMAGE_PATTERN, index)[0]
    dest = os.path.join(STORE, "images", iso)
    if os.path.exists(dest) or plan:
        return dest
    sums = urllib.request.urlopen(IMAGE_INDEX + "SHA256SUMS", timeout=30).read().decode()
    expected = re.search(r"([0-9a-f]{64})\s+" + re.escape(iso), sums)[1]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    digest, part = hashlib.sha256(), dest + ".part"
    with urllib.request.urlopen(IMAGE_INDEX + iso, timeout=60) as r, open(part, "wb") as f:
        total, done = int(r.headers.get("Content-Length", 0)), 0
        while chunk := r.read(1 << 20):
            f.write(chunk); digest.update(chunk); done += len(chunk)
            if total:
                print(f"\rdownloading {iso}: {done * 100 // total}%", end="", file=sys.stderr)
    print(file=sys.stderr)
    if digest.hexdigest() != expected:
        os.unlink(part)
        raise SystemExit(f"checksum mismatch for {iso}; download discarded")
    os.rename(part, dest)
    return dest


def up(name, iso=None, no_view=False, plan=False, **opts):
    exists = os.path.exists(os.path.join(vm_dir(name), "machine.json"))
    st = state(name) if exists else "stopped"
    steps = []
    if not exists:
        steps += ([] if iso else ["download Debian live XFCE (~4 GB, SHA-256 verified)"]) + [f"create {name}"]
    if st == "stopped":
        steps.append(f"start {name}")
    elif st == "paused":
        steps.append(f"resume {name}")
    if not no_view:
        steps.append(f"view {name}")
    if plan:
        image = None if exists else iso or default_image(plan=True)
        print(json.dumps({"plan": steps, "image": image}, indent=1)); return
    if not exists:
        create(name, iso or default_image(), **opts)
    if st == "stopped":
        start(name)
    elif st == "paused":
        pause(name, resume=True)
    if not no_view and sys.stdin.isatty():
        view(name)
    else:
        print(json.dumps({"ready": name, "next": f"terminal-linux view {name}  (or: terminal-linux mcp {name})"}))


def doctor():
    checks = []
    def check(name, ok, detail, required=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "required": required})
    qemu = shutil.which("qemu-system-x86_64")
    ver = subprocess.run([qemu, "--version"], capture_output=True, text=True).stdout.split("\n")[0] if qemu else ""
    num = tuple(int(x) for x in re.search(r"(\d+)\.(\d+)", ver).groups()) if ver else (0, 0)
    check("qemu", num >= (7, 1), ver or "qemu-system-x86_64 not found (apt install qemu-system-x86)")
    check("qemu-img", shutil.which("qemu-img"), "found" if shutil.which("qemu-img") else "apt install qemu-utils")
    check("kvm", os.access("/dev/kvm", os.R_OK | os.W_OK),
          "ok" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "no access to /dev/kvm: sudo usermod -aG kvm $USER, then log in again")
    check("python", sys.version_info >= (3, 8), sys.version.split()[0])
    viewer = next((e for e, _ in VIEWERS if shutil.which(e)), None)
    check("window viewer", viewer, viewer or "optional: apt install tigervnc-viewer", required=False)
    check("terminal graphics", kitty_supported(), "Kitty graphics available" if kitty_supported()
          else "optional: run view in Kitty, Ghostty or WezTerm", required=False)
    print(json.dumps(checks, indent=1))
    if not all(c["ok"] for c in checks if c["required"]):
        sys.exit(1)


def skills(cmd, dest=None):
    if cmd == "get":
        with open(SKILL) as f:
            print(f.read(), end=""); return
    dest = os.path.join(dest or os.path.expanduser("~/.claude/skills"), "terminal-linux")
    os.makedirs(dest, exist_ok=True)
    shutil.copy(SKILL, os.path.join(dest, "SKILL.md"))
    print(json.dumps({"installed": os.path.join(dest, "SKILL.md")}))


# ---------- MCP ----------

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}

_N = {"type": "number"}
_SHOT = {"screenshot": {"type": "boolean", "description": "Return a fresh screenshot with the result"}}
TOOLS = [
    ("screenshot", "Capture the guest Linux screen. Coordinates in other tools are pixels of this image.", _schema({})),
    ("click", "Click at a point.", _schema({"x": _N, "y": _N, "clicks": {"type": "integer", "enum": [1, 2, 3]},
                                             "button": {"type": "string", "enum": ["left", "right", "middle"]}, **_SHOT}, ["x", "y"])),
    ("move", "Move the pointer.", _schema({"x": _N, "y": _N, **_SHOT}, ["x", "y"])),
    ("drag", "Drag with the left button.", _schema({"x": _N, "y": _N, "x2": _N, "y2": _N, **_SHOT}, ["x", "y", "x2", "y2"])),
    ("scroll", "Scroll; negative dy scrolls down (100 per notch).", _schema({"dy": _N, "x": _N, "y": _N, **_SHOT}, ["dy"])),
    ("type", "Type literal text in the guest's keyboard layout (Unicode beyond it uses Ctrl+Shift+U).",
     _schema({"text": {"type": "string"}, **_SHOT}, ["text"])),
    ("key", "Press a key chord, e.g. ctrl+a, alt+tab, return, super.", _schema({"key": {"type": "string"}, **_SHOT}, ["key"])),
    ("status", "VM run state, screen size and logged-in guest users.", _schema({})),
]
INPUT_TOOLS = {"click", "move", "drag", "scroll", "type", "key"}


def image(data):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def mcp(name):
    agent = None  # whether qemu-guest-agent answered once; unknown until asked

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None:
            continue  # notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "terminal-linux", "version": VERSION}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}})
        elif method == "tools/call":
            p = req.get("params", {})
            tool, a = p.get("name"), p.get("arguments") or {}
            try:
                if tool in INPUT_TOOLS and agent is not False:
                    users = guest_users(name)
                    agent = users is not None
                    if users == []:
                        raise RuntimeError("The guest has no logged-in session (login screen). Ask the user to sign in: "
                                           f"terminal-linux view {name}")
                out = act(name, tool, a)
                if tool == "screenshot":
                    content = [image(out[0])]
                else:
                    content = [{"type": "text", "text": json.dumps(out)}]
                    if a.get("screenshot"):
                        time.sleep(0.3); content.append(image(screenshot(name)[0]))
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": content}})
            except Exception as e:  # report tool failures to the agent instead of dying
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


# ---------- CLI ----------

def main(argv=None):
    p = argparse.ArgumentParser(prog="terminal-linux", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"terminal-linux {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def sizes(sp):
        sp.add_argument("--cpus", type=int, default=4); sp.add_argument("--memory", type=int, default=4, help="GiB")
        sp.add_argument("--disk", type=int, default=32, help="GiB"); sp.add_argument("--width", type=int, default=1280)
        sp.add_argument("--height", type=int, default=800); sp.add_argument("--layout", default="us", choices=sorted(LAYOUTS))
        sp.add_argument("--offline", action="store_true")
    u = sub.add_parser("up"); u.add_argument("name", nargs="?", default="default"); u.add_argument("--iso")
    u.add_argument("--no-view", action="store_true"); u.add_argument("--plan", action="store_true"); sizes(u)
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("--iso"); sizes(c)
    s = sub.add_parser("start"); s.add_argument("name"); s.add_argument("--window", action="store_true")
    t = sub.add_parser("stop"); t.add_argument("name"); t.add_argument("--force", action="store_true")
    t.add_argument("--timeout", type=int, default=60)
    for n in ("status", "view", "show", "hide", "pause", "resume"):
        sub.add_parser(n).add_argument("name")
    sub.add_parser("ls"); sub.add_parser("doctor")
    m = sub.add_parser("mcp"); m.add_argument("name", nargs="?", default="default")
    k = sub.add_parser("skills"); k.add_argument("skill_cmd", choices=["get", "install"]); k.add_argument("--dir")
    a = sub.add_parser("action"); a.add_argument("name"); a.add_argument("tool"); a.add_argument("args", nargs="*")
    a.add_argument("--output"); a.add_argument("--button", default="left"); a.add_argument("--double", action="store_true")
    a.add_argument("--dy", type=int); a.add_argument("--x", type=float); a.add_argument("--y", type=float)
    o = p.parse_args(argv)

    opts = {k: getattr(o, k) for k in ("cpus", "memory", "disk", "width", "height", "layout", "offline")} if o.cmd in ("up", "create") else {}
    try:
        if o.cmd == "up": return up(o.name, o.iso, o.no_view, o.plan, **opts)
        if o.cmd == "create": return create(o.name, o.iso or default_image(), **opts)
        if o.cmd == "start": return start(o.name, o.window)
        if o.cmd == "stop": return stop(o.name, o.force, o.timeout)
        if o.cmd in ("pause", "resume"): return pause(o.name, o.cmd == "resume")
        if o.cmd == "ls": return ls()
        if o.cmd == "doctor": return doctor()
        if o.cmd == "view": return view(o.name)
        if o.cmd == "show": return show(o.name)
        if o.cmd == "hide": return hide(o.name)
        if o.cmd == "mcp": return mcp(o.name)
        if o.cmd == "skills": return skills(o.skill_cmd, o.dir)
        if o.cmd == "status":
            print(json.dumps(act(o.name, "status", {}))); return
        nums = [float(v) for v in o.args] if o.tool in ("click", "move", "drag") else []
        args = {"screenshot": {}, "status": {},
                "click": lambda: {"x": nums[0], "y": nums[1], "button": o.button, "clicks": 2 if o.double else 1},
                "move": lambda: {"x": nums[0], "y": nums[1]},
                "drag": lambda: dict(zip(("x", "y", "x2", "y2"), nums)),
                "scroll": lambda: {k: v for k, v in (("dy", o.dy), ("x", o.x), ("y", o.y)) if v is not None},
                "type": lambda: {"text": " ".join(o.args)},
                "key": lambda: {"key": o.args[0]}}[o.tool]
        out = act(o.name, o.tool, args() if callable(args) else args)
        if o.tool == "screenshot":
            data, (w, h) = out
            dest = o.output or os.path.join(vm_dir(o.name), "screen.png")
            if os.path.abspath(dest) != os.path.join(vm_dir(o.name), "screen.png"):
                with open(dest, "wb") as f: f.write(data)
            out = {"path": os.path.abspath(dest), "width": w, "height": h}
        print(json.dumps(out))
    except (RuntimeError, ValueError, KeyError, IndexError, OSError) as e:
        print(json.dumps({"error": str(e) or type(e).__name__}), file=sys.stderr); sys.exit(1)


if __name__ == "__main__":
    main()
