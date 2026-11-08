#!/usr/bin/env python3
"""terminal-vm: a separate desktop VM for computer-use agents, viewed from a terminal.

One command and one MCP server for every guest. Linux and Windows guests (XP to Server 2025,
`--os`, installed unattended) run in QEMU on Linux (KVM) and Windows (WHPX) hosts: the host
drives QEMU over QMP, nothing runs inside the guest, the host desktop is never touched, stdlib
only. macOS guests run on a Mac only: there terminal-vm hands them to terminal-mac (Apple
Virtualization), which is also the default for `up` and `create` without `--os`.

  up [NAME] [--os OS] [--iso PATH] [--no-view] [--plan]   guided: image, create, start, view
  create NAME [--os linux|winxp|win7|win8|win10|win11|win2016|win2019|win2022|win2025|macos]
              [--iso PATH] [--product-key KEY] [--cpus N] [--memory GiB] [--disk GiB]
              [--width 1280] [--height 800] [--layout us|fr] [--offline]
  ls | status NAME | doctor | help | version
  start NAME [--window] | stop NAME [--force] [--timeout 60] | pause NAME | resume NAME
  rm NAME | reset NAME     delete a stopped VM | blank its disk to reinstall
  clone SRC NEW            instant copy-on-write copy of a stopped VM (SRC then stays read-only)
  snapshot NAME save|load|rm TAG | snapshot NAME ls
  display NAME --width W --height H    screen size at the next start
  net create|rm NET | net ls | net attach|detach NAME NET   isolated VM-to-VM networks (stopped VMs)
  internet NAME [on|off]   NAT internet access (for a sandboxed VM, on = the user's approval)
  top                      running VMs' real CPU and memory on the host, allotments, quota
  quota [--cpus N --memory GiB --disk GiB --vms N]   host-wide ceiling for all VMs (default from the host)
  resources NAME [--cpus N] [--memory GiB]   within the VM's ceiling (create --max-cpus/--max-memory); live where possible
  sandbox NAME [on|off]   no network, no guest agent or file copy, disk reset at each stop, actions logged
  layout NAME [us|fr]      keyboard layout for type/key (us = QWERTY, fr = AZERTY); action ... --layout: one call
  image [--os OS] [--plan]             download the installation image only
  push NAME LOCAL GUEST | pull NAME GUEST LOCAL   files, through qemu-guest-agent
  view NAME [--read-only]  terminal viewer (Kitty or Sixel: Windows Terminal, foot); Ctrl+] exits, Ctrl+O window
  show NAME [--read-only] | hide NAME   native window (VNC viewer)
  action NAME screenshot|click|move|drag|scroll|type|key|wait ...
  mcp [NAME]         stdio MCP server
  skills get | skills path | skills install [--dir DIR]
"""
import argparse, base64, collections, contextlib, hashlib, io, json, os, queue, re, secrets, shutil, signal, socket, struct, subprocess, sys, \
    threading, time, urllib.parse, urllib.request
from xml.sax.saxutils import escape

WINDOWS = os.name == "nt"
if not WINDOWS:
    import fcntl, termios, tty  # terminal viewer only

VERSION = "0.4.0"
_DATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~")) if WINDOWS else \
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
# VMs made before the rename to terminal-vm stay where they are: the old folder is used until a new one exists
STORE = os.environ.get("TERMINAL_VM_HOME") or os.environ.get("TERMINAL_LINUX_HOME") or next(
    (p for p in (os.path.join(_DATA, n) for n in ("terminal-vm", "terminal-linux")) if os.path.isdir(p)),
    os.path.join(_DATA, "terminal-vm"))
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, "skills", "terminal-vm", "SKILL.md")
ABS_MAX = 0x7FFF  # usb-tablet absolute axis range
IMAGE_INDEX = "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/"
IMAGE_PATTERN = r"debian-live-[\d.]+-amd64-xfce\.iso"


def vm_dir(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", name or "") or name == "images":
        raise SystemExit(f"invalid VM name: {name!r} (letters, digits, _ and -)")
    return os.path.join(STORE, name)


def endpoint(name, kind):
    """Where QEMU listens for `kind` (qmp, qmp-view, qga, vnc): a unix socket in the private VM
    directory, so only the owner can reach it. Exception: VNC on Windows hosts, whose viewers only
    speak TCP: a localhost port chosen at start, behind a per-start VNC password."""
    if not (WINDOWS and kind == "vnc"):
        return os.path.join(vm_dir(name), kind + ".sock")
    try:
        with open(os.path.join(vm_dir(name), "ports.json")) as f:
            return ("127.0.0.1", json.load(f)[kind])
    except (FileNotFoundError, KeyError):
        return ("127.0.0.1", 1)  # never started: connecting fails like a missing socket


def _win_unix(path, timeout):
    """AF_UNIX client on Windows: Winsock and QEMU support it (Windows 10 1803+), Python's socket module does not."""
    import ctypes
    ws = ctypes.WinDLL("ws2_32")
    ws.socket.restype = ctypes.c_size_t  # SOCKET is a UINT_PTR
    ws.connect.argtypes, ws.closesocket.argtypes = (ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int), (ctypes.c_size_t,)
    h = ws.socket(1, socket.SOCK_STREAM, 0)
    if h == ctypes.c_size_t(-1).value:
        raise OSError("AF_UNIX sockets unavailable (Windows 10 1803 or later required)")
    addr = struct.pack("H108s", 1, path.encode())
    if ws.connect(h, addr, len(addr)):
        err = ws.WSAGetLastError()
        ws.closesocket(h)
        raise OSError(f"cannot connect to {path} (WSA error {err})")
    s = socket.socket(fileno=h)
    s.settimeout(timeout)
    return s


def connect(addr, timeout):
    if WINDOWS and isinstance(addr, str):
        return _win_unix(addr, timeout)
    s = socket.socket(socket.AF_INET if isinstance(addr, tuple) else socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(addr)
    except OSError:
        s.close()
        raise
    return s


def free_ports(n):
    """n distinct free ports: all sockets stay bound until every port is picked."""
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def qemu_bin(tool):
    """QEMU executables from PATH, or the default Windows install folder."""
    exe = tool + (".exe" if WINDOWS else "")
    for candidate in (shutil.which(tool), os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "qemu", exe)):
        if candidate and os.path.isfile(candidate):
            return candidate
    raise SystemExit(f"{tool} not found: install QEMU (Linux: apt install qemu-system-x86 qemu-utils; "
                     "Windows: winget install SoftwareFreedomConservancy.QEMU)")


def load(name):
    try:
        with open(os.path.join(vm_dir(name), "machine.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"no VM named {name!r}; create it with: terminal-vm up {name}")


# ---------- QMP ----------

class QMP:
    def __init__(self, addr):
        try:
            self.sock = connect(addr, 10)
        except OSError as e:
            raise RuntimeError("VM is not running (cannot reach its QMP socket)") from e
        self.buf = b""
        try:
            self._recv()  # greeting; times out while another client holds QEMU's single QMP connection
            self.cmd("qmp_capabilities")
        except OSError as e:
            self.sock.close()
            raise RuntimeError("QMP busy or not answering (another command is using this VM)") from e

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
    return QMP(endpoint(name, "qmp"))


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
        self.sock = connect(endpoint(name, "qga"), timeout)
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

    def pull(self, path, mode="r"):
        h = self.call("guest-file-open", path=path, mode=mode)
        data = bytearray()  # shortcut: whole file in memory, fine below a few hundred MB; stream to disk if larger
        try:
            while True:
                r = self.call("guest-file-read", handle=h, count=1 << 20)
                data += base64.b64decode(r["buf-b64"])
                if r.get("eof") or not r.get("count"):
                    return bytes(data)
        finally:
            self.call("guest-file-close", handle=h)

    def push(self, path, data):
        h = self.call("guest-file-open", path=path, mode="wb")
        try:
            for i in range(0, max(len(data), 1), 1 << 20):
                self.call("guest-file-write", handle=h, **{"buf-b64": base64.b64encode(data[i:i + (1 << 20)]).decode()})
        finally:
            self.call("guest-file-close", handle=h)

    def read(self, path):
        return self.pull(path).decode()


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


def hex_numpad_events(ch, layout):
    """Windows Unicode entry: hold Alt, numpad +, hex code, release Alt. Needs EnableHexNumpad,
    which the unattended install sets; works in most Win32 apps."""
    keys = ["kp_add"] + [f"kp_{h}" if h.isdigit() else LAYOUTS[layout][h][0][-1] for h in f"{ord(ch):x}"]
    ev = lambda k, d: {"type": "key", "data": {"down": d, "key": {"type": "qcode", "data": k}}}
    return [ev("alt", True)] + [e for k in keys for e in (ev(k, True), ev(k, False))] + [ev("alt", False)]


def type_text(q, text, layout, family="linux"):
    seqs = []
    for c in text:  # validate everything before typing anything
        if family == "windows" and c not in COMMON and c not in LAYOUTS[layout]:
            if not c.isprintable():
                raise ValueError(f"cannot type control character {c!r}; use the key tool")
            seqs.append(hex_numpad_events(c, layout))
        else:
            seqs.append(char_keys(c, layout))
    for seq in seqs:
        if seq and isinstance(seq[0], dict):
            q.cmd("input-send-event", events=seq); time.sleep(0.02)
        else:
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
        raise RuntimeError("VM is paused; run: terminal-vm resume " + name)
    if tool == "wait":  # until two screenshots `stable` seconds apart are identical, or timeout
        start, last, since = time.time(), None, time.time()
        while True:
            data, _ = screen_pixels(name, q)  # raw PPM: no PNG compression on every poll
            if data != last:
                last, since = data, time.time()
            elif time.time() - since >= float(a.get("stable", 1)):
                return {"stable": True, "waited": round(time.time() - start, 1)}
            if time.time() - start >= float(a.get("timeout", 30)):
                return {"stable": False, "waited": round(time.time() - start, 1)}  # a blinking cursor never settles
            time.sleep(0.25)
    m = load(name)
    layout, family = a.get("layout") or m.get("layout", "us"), PROFILES[m.get("os", "linux")]["family"]
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; choose from {', '.join(LAYOUTS)}")
    if tool in ("click", "move", "drag", "scroll"):
        _, size = screen_pixels(name, q)  # current resolution, to scale into the tablet range (raw: no PNG to encode)
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
        type_text(q, a["text"], layout, family)
    elif tool == "key":
        send_keys(q, chord_keys(a["key"], layout))
    else:
        raise ValueError(f"unknown action {tool!r}")
    return {"ok": True}


# ---------- VM lifecycle ----------

# Guest hardware per OS: only devices that OS drives without extra drivers.
# machine pc = i440fx (IDE, UHCI, floppy) for XP; q35 = AHCI (its "ide" bus) for Windows 7+.
_WIN = dict(family="windows", machine="q35", disk_bus="ide", nic="e1000e", vga="VGA", usb="qemu-xhci",
            cpus=2, memory=4, disk=64, unattend="xml", key=None, index=None, oobe="modern")
PROFILES = {
    "linux": dict(family="linux", machine="q35", disk_bus="virtio", nic="virtio-net-pci", vga="virtio-vga",
                  usb="qemu-xhci", cpus=4, memory=4, disk=32, unattend=None),
    "winxp": dict(_WIN, machine="pc", nic="rtl8139", vga="cirrus-vga", usb="uhci", cpus=1, memory=1, disk=16,
                  unattend="sif", key="required", cpu="qemu64",  # XP dislikes host CPU passthrough
                  max_cpus=2, max_memory=3),  # 32-bit XP: two sockets, ~3.5 GB of addressable RAM
    "win7": dict(_WIN, nic="e1000", usb="usb-ehci", memory=2, disk=32, oobe="win7"),
    "win8": dict(_WIN, nic="e1000", memory=2, disk=32, key="required"),
    # Microsoft's public KMS client setup keys: they select the Pro edition, they do not activate it.
    "win10": dict(_WIN, key="W269N-WFGWX-YVC9B-4J6C9-T83GX"),
    "win11": dict(_WIN, key="W269N-WFGWX-YVC9B-4J6C9-T83GX", bypass=True),
    # Evaluation media need no key; image 2 is Standard with the Desktop Experience.
    **{f"win{v}": dict(_WIN, memory=4, disk=64, index=2) for v in ("2016", "2019", "2022", "2025")},
}
TEMPLATE_MAX = (8, 16)  # default ceiling per VM (CPUs, GiB) when a template sets none; the host caps it further


def host_memory_gb():
    if WINDOWS:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                       [(n, ctypes.c_ulonglong) for n in ("total", "avail", "pt", "pa", "vt", "va", "ve")]
        st = Status(); st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.total // 2 ** 30
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2 ** 30


def _quota_file():
    try:
        with open(os.path.join(STORE, "quota.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def limits(m):
    """(max CPUs, max GiB) a VM may use: chosen at create, else its template's; never above the host or the
    quota (a bigger ceiling could never start)."""
    p, q = PROFILES[m.get("os", "linux")], _quota_file()
    cpus = m.get("max_cpus") or p.get("max_cpus", TEMPLATE_MAX[0])
    mem = m.get("max_memory_mb", 0) // 1024 or p.get("max_memory", TEMPLATE_MAX[1])
    return (max(1, min(cpus, os.cpu_count(), q.get("cpus", max(1, os.cpu_count() - 1)))),
            max(1, min(mem, host_memory_gb(), q.get("memory_gb", max(1, host_memory_gb() * 3 // 4)))))


def hot_cpus(m):
    return PROFILES[m.get("os", "linux")]["family"] == "linux"  # Windows Standard and older cannot add CPUs live


WINDOWS_IMAGES = {  # official evaluation media that can be downloaded without a sign-in
    "win2025": "https://go.microsoft.com/fwlink/?linkid=2293312&clcid=0x409&culture=en-us&country=us",
}
LOCALES = {"us": ("en-US", "0409:00000409"), "fr": ("fr-FR", "040c:0000040c")}


# Guest agent for status users, push and pull: the answers drive's letter varies, so look for the installer.
# Without it on the drive (older Windows, or offline host) the loop finds nothing and does nothing.
GUEST_TOOLS_CMD = ('cmd /c for %d in (D E F G H I) do if exist %d:\\virtio-win-guest-tools.exe '
                   'start /wait %d:\\virtio-win-guest-tools.exe /install /quiet /norestart')


def autounattend(p, layout, user, password, key, ui="en-US"):
    """autounattend.xml for Windows 7 and later: wipe disk 0, install, local admin with auto-logon,
    skip OOBE, enable hex Unicode entry (Alt, numpad +, hex) for the type tool. `ui` must be a
    language present on the installation media, or Setup stops at its language page."""
    lang, kb = LOCALES[layout]
    comp = lambda c, body: (f'<component name="{c}" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" '
                            f'language="neutral" versionScope="nonSxS">{body}</component>')
    intl = lambda: f"<InputLocale>{kb}</InputLocale><SystemLocale>{lang}</SystemLocale><UILanguage>{escape(ui)}</UILanguage><UserLocale>{lang}</UserLocale>"
    bypass = "".join(f'<RunSynchronousCommand wcm:action="add"><Order>{i}</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig '
                     f'/v {v} /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>'
                     for i, v in enumerate(("BypassTPMCheck", "BypassSecureBootCheck", "BypassCPUCheck"), 1))
    image = (f"<InstallFrom><MetaData wcm:action=\"add\"><Key>/IMAGE/INDEX</Key><Value>{p['index']}</Value></MetaData></InstallFrom>"
             if p["index"] else "")
    product = f"<ProductKey><Key>{escape(key)}</Key><WillShowUI>OnError</WillShowUI></ProductKey>" if key else ""
    setup = (("<RunSynchronous>" + bypass + "</RunSynchronous>" if p.get("bypass") else "") +
             "<DiskConfiguration><Disk wcm:action=\"add\"><DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk><CreatePartitions>"
             "<CreatePartition wcm:action=\"add\"><Order>1</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>"
             "</CreatePartitions><ModifyPartitions><ModifyPartition wcm:action=\"add\"><Order>1</Order><PartitionID>1</PartitionID>"
             "<Format>NTFS</Format><Label>Windows</Label><Active>true</Active></ModifyPartition>"  # C: may be the answers drive during Setup
             "</ModifyPartitions></Disk></DiskConfiguration>"
             f"<ImageInstall><OSImage>{image}<InstallTo><DiskID>0</DiskID><PartitionID>1</PartitionID></InstallTo></OSImage></ImageInstall>"
             f"<UserData><AcceptEula>true</AcceptEula>{product}</UserData>")
    pw = f"<Password><Value>{escape(password)}</Value><PlainText>true</PlainText></Password>"
    oobe = ("<OOBE><HideEULAPage>true</HideEULAPage><NetworkLocation>Work</NetworkLocation><ProtectYourPC>3</ProtectYourPC>"
            + ("<SkipMachineOOBE>true</SkipMachineOOBE><SkipUserOOBE>true</SkipUserOOBE>" if p["oobe"] == "win7" else
               "<HideOEMRegistrationScreen>true</HideOEMRegistrationScreen><HideOnlineAccountScreens>true</HideOnlineAccountScreens>"
               "<HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>") + "</OOBE>")
    # An agent's VM must stay visible: no display-off, sleep or lock on idle (the screen went black, then locked).
    machine = "".join(f'<RunSynchronousCommand wcm:action="add"><Order>{i}</Order><Path>{c}</Path></RunSynchronousCommand>'
                      for i, c in enumerate(('powercfg /change monitor-timeout-ac 0',
                                            'powercfg /change standby-timeout-ac 0',
                                            'powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0'), 1))
    # EnableHexNumpad is read at logon: set it in the first session, then log off so the session the agent gets
    # (auto-logon again, forced) has it. A reboot did the same but cost 8 min of servicing ("Updates are underway"). Writing it to the Default profile in specialize was tried and did not reach the user.
    # DisableLockWorkstation: a locked session ignores the ACPI power button (stop would hang), so Win+L and the
    # Lock entry are removed. The Policies key needs admin rights (denied in a non-elevated prompt).
    first = ("<FirstLogonCommands>" + "".join(
        f'<SynchronousCommand wcm:action="add"><Order>{i}</Order><CommandLine>{c}</CommandLine></SynchronousCommand>'
        for i, c in enumerate(('reg add "HKCU\\Control Panel\\Input Method" /v EnableHexNumpad /t REG_SZ /d 1 /f',
                               GUEST_TOOLS_CMD,
                               'reg add HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Policies\\System '
                               '/v DisableLockWorkstation /t REG_DWORD /d 1 /f',
                               # without it Winlogon leaves a logged-off session at the Ctrl+Alt+Delete screen
                               'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" '
                               '/v ForceAutoLogon /t REG_SZ /d 1 /f',
                               'shutdown /l'), 1)) + "</FirstLogonCommands>")
    shell = "Microsoft-Windows-Shell-Setup"
    return ('<?xml version="1.0" encoding="utf-8"?>\n<unattend xmlns="urn:schemas-microsoft-com:unattend" '
            'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">'
            '<settings pass="windowsPE">' + comp("Microsoft-Windows-International-Core-WinPE",
                                                 f"<SetupUILanguage><UILanguage>{escape(ui)}</UILanguage></SetupUILanguage>" + intl())
            + comp("Microsoft-Windows-Setup", setup) + '</settings>'
            '<settings pass="specialize">' + comp(shell, "<ComputerName>*</ComputerName>")
            + comp("Microsoft-Windows-Deployment", f"<RunSynchronous>{machine}</RunSynchronous>") + '</settings>'
            '<settings pass="oobeSystem">' + comp("Microsoft-Windows-International-Core", intl())
            # AdministratorPassword: Server OOBE otherwise stops on the built-in Administrator's password page
            + comp(shell, f'<UserAccounts><AdministratorPassword><Value>{escape(password)}</Value><PlainText>true</PlainText>'
                          f'</AdministratorPassword><LocalAccounts><LocalAccount wcm:action="add">{pw}<Name>{escape(user)}</Name>'
                          f'<Group>Administrators</Group></LocalAccount></LocalAccounts></UserAccounts>'
                          f'<AutoLogon>{pw}<Enabled>true</Enabled><LogonCount>9999</LogonCount><Username>{escape(user)}</Username></AutoLogon>'
                          + oobe + first) + '</settings></unattend>\n')


def winnt_sif(layout, password, key):
    """Unattended answer file for Windows XP setup, read from the floppy."""
    return "\r\n".join([
        "[Data]", "AutoPartition=1", 'MsDosInitiated="0"', 'UnattendedInstall="Yes"',
        "[Unattended]", "UnattendMode=FullUnattended", "OemSkipEula=Yes", "OemPreinstall=No",
        "TargetPath=\\WINDOWS", "Repartition=Yes", 'UnattendSwitch="Yes"', 'WaitForReboot="No"',
        "[GuiUnattended]", f'AdminPassword="{password}"', "EncryptedAdminPassword=No", "AutoLogon=Yes",
        "AutoLogonCount=9999", "OEMSkipRegional=1", "OemSkipWelcome=1", "TimeZone=85",
        "[UserData]", f"ProductKey={key}", 'FullName="user"', 'OrgName=""', "ComputerName=*",
        "[RegionalSettings]", f"InputLocale={LOCALES[layout][1]}",
        "[Identification]", "JoinWorkgroup=WORKGROUP", "[Networking]", "InstallDefaultComponents=Yes",
        "[GuiRunOnce]", 'Command0="reg add \\"HKCU\\Control Panel\\Input Method\\" /v EnableHexNumpad /t REG_SZ /d 1 /f"', ""])


def accelerators():
    """KVM on Linux, WHPX on Windows; QEMU falls back to the next -accel (TCG: works, much slower)."""
    if WINDOWS:
        return ["-accel", "whpx,kernel-irqchip=off", "-accel", "tcg"], "max"
    return (["-accel", "kvm"], "host") if os.access("/dev/kvm", os.R_OK | os.W_OK) else (["-accel", "tcg"], "max")


HV_FLAGS = ",hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_vpindex,hv_runtime,hv_time,hv_synic,hv_stimer,hv_frequencies"


def installing(d, m):
    """Windows answers present and no clean shutdown yet (the first one marks the VM installed)."""
    return os.path.isdir(os.path.join(d, "answers")) and not m.get("installed")


def run_cpus(name, m):
    """vCPUs the VM boots with: its ceiling while Windows installs (when the quota had room at start)."""
    return m.get("install_cpus", m["cpus"]) if installing(vm_dir(name), m) else m["cpus"]


def qemu_args(name, m, window, vnc_port=None):
    """Full QEMU command line for a VM (pure: no I/O), so every OS profile is testable.
    vnc_port: Windows hosts, VNC on localhost TCP with a password (set over QMP after start)."""
    d, p = vm_dir(name), PROFILES[m.get("os", "linux")]
    accel, cpu = accelerators()
    if p["family"] == "windows" and cpu == "host":  # KVM: Hyper-V enlightenments (timers, APIC, spinlocks) for Windows
        cpu += HV_FLAGS
    cpu = p.get("cpu", cpu)
    ep = lambda k: f"unix:{os.path.join(d, k + '.sock')}"
    w, h = m.get("width", 1280), m.get("height", 800)
    # Sandbox: no network, no guest agent channel, disk writes thrown away at stop (once installed), QEMU confined
    # by seccomp where it has it (Linux hosts). Only the screen and input reach the guest.
    sandbox, setup = m.get("sandbox"), installing(d, m)
    vga = p["vga"] if p["vga"] == "cirrus-vga" else f"{p['vga']},xres={w},yres={h}"  # cirrus has no preferred mode
    max_cpus, max_gb = limits(m)
    # Balloon: the guest gets its ceiling of RAM and gives back what is above memory_mb, so memory can change
    # live. Only where the guest has the driver (Linux; Windows with the virtio-win tools), or it would keep it all.
    ram = vm_ram_mb(m)
    smp = f"{m['cpus']},maxcpus={max(max_cpus, m['cpus'])}" if hot_cpus(m) else str(run_cpus(name, m))
    args = ["-name", name, *accel, "-cpu", cpu, "-machine", p["machine"], "-m", str(ram), "-smp", smp,
            *(["-device", "virtio-balloon-pci,free-page-reporting=on"] if m.get("balloon") else []),
            # QEMU locks the image: one VM per disk. During an unattended install, ignore guest flushes (much faster):
            # a host crash then loses the install, never a finished VM.
            "-drive", f"file={os.path.join(d, 'disk.qcow2')},if={p['disk_bus']}" + (",cache=unsafe" if setup else "")
            + (",snapshot=on" if sandbox and not setup else ""),
            "-cdrom", m["iso"], "-boot", "order=cd", "-device", vga,
            *(["-usb"] if p["usb"] == "uhci" else ["-device", p["usb"]]), "-device", "usb-tablet",
            "-nic", "none" if m.get("offline") or (sandbox and not m.get("internet")) else f"user,model={p['nic']}",
            *net_args(m, p["nic"]),
            *([] if sandbox else ["-device", "virtio-serial-pci",
                                  "-chardev", "socket,path=" + os.path.join(d, "qga.sock") + ",server=on,wait=off,id=qga0",
                                  "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0"]),
            *(["-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny"]
              if sandbox and not WINDOWS else []),  # QEMU for Windows is built without seccomp
            "-qmp", ep("qmp") + ",server=on,wait=off",
            "-qmp", ep("qmp-view") + ",server=on,wait=off",  # the viewer's own monitor
            # VNC takes a display number; password=on refuses every client until the password is set
            "-vnc", ep("vnc") if vnc_port is None else f"127.0.0.1:{vnc_port - 5900},password=on",
            "-pidfile", os.path.join(d, "qemu.pid"), "-display", ("sdl" if WINDOWS else "gtk") if window else "none"]
    answers = os.path.join(d, "answers")
    if sandbox and not setup:
        return args  # the answers drive is only for Setup
    if p["unattend"] == "xml" and os.path.isdir(answers):  # Setup reads autounattend.xml from removable drives
        # removable=on: Setup only searches removable media; a fixed USB disk is ignored
        args += ["-drive", f"if=none,id=answers,driver=vvfat,dir={answers},rw=off", "-device", "usb-storage,drive=answers,removable=on"]
    elif p["unattend"] == "sif" and os.path.isdir(answers):  # XP Setup reads winnt.sif from the floppy
        args += ["-drive", f"if=floppy,index=0,driver=vvfat,dir={answers},floppy=on"]
    return args


def create(name, iso, os_name="linux", cpus=None, memory=None, disk=None, width=1280, height=800, layout="us",
           offline=False, product_key=None, ui_language="en-US", sandbox=False, max_cpus=None, max_memory=None):
    d = vm_dir(name)
    p = PROFILES.get(os_name)
    if p is None:
        raise SystemExit(f"unknown --os {os_name!r}; choose from {', '.join(PROFILES)}")
    cpus, memory, disk = cpus or min(p["cpus"], os.cpu_count()), memory or p["memory"], disk or p["disk"]
    if os.path.exists(os.path.join(d, "machine.json")):
        raise SystemExit(f"VM {name!r} already exists in {d}")
    if not iso or not os.path.isfile(iso):
        raise SystemExit(f"ISO not found: {iso}")
    if layout not in LAYOUTS:
        raise SystemExit(f"unknown layout {layout!r}; choose from {', '.join(LAYOUTS)}")
    # Default memory ceiling: twice the allocation, at least 2 GiB of headroom: with a balloon the ceiling is what
    # the quota counts, so the template's maximum by default would leave room for a single VM.
    top = limits({"os": os_name, "max_cpus": max_cpus,
                  "max_memory_mb": (max_memory or max(2 * memory, memory + 2)) * 1024})
    if (max_cpus or 0) > os.cpu_count() or (max_memory or 0) > host_memory_gb():
        raise SystemExit(f"--max-cpus/--max-memory above this host ({os.cpu_count()} CPUs, {host_memory_gb()} GiB)")
    if cpus > top[0] or memory > top[1]:
        raise SystemExit(f"--cpus/--memory above the VM's ceiling of {top[0]} CPUs and {top[1]} GiB "
                         "(raise it with --max-cpus/--max-memory)")
    if not (1 <= cpus <= os.cpu_count() and 1 <= memory <= 512 and 4 <= disk and 640 <= width <= 7680 and 480 <= height <= 4320):
        raise SystemExit("invalid size: check --cpus (<= host CPUs), --memory GiB, --disk GiB (>= 4), --width/--height")
    key = product_key or (p.get("key") if p.get("key") != "required" else None)
    if p.get("key") == "required" and not key:
        raise SystemExit(f"{os_name} setup needs your product key: --product-key XXXXX-XXXXX-XXXXX-XXXXX-XXXXX")
    if key and not re.fullmatch(r"[A-Za-z0-9]{5}(-[A-Za-z0-9]{5}){4}", key):
        raise SystemExit("product key format: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX")
    lim = quota_limits()[0]
    if cpus > lim["cpus"] or top[1] > lim["memory_gb"]:
        raise SystemExit(f"this VM could never start within the quota ({lim['cpus']} CPUs, {lim['memory_gb']} GiB "
                         f"for its memory ceiling of {top[1]}): lower --cpus/--max-memory, or raise the quota")
    with quota_lock():
        quota_check({"disk_gb": disk})
        os.makedirs(d, mode=0o700, exist_ok=True)  # under the lock: the disk now counts for the next check
        with open(os.path.join(d, "machine.json"), "w") as f:
            json.dump({"os": os_name, "memory_mb": memory * 1024, "cpus": cpus, "disk_gb": disk}, f)
    subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", os.path.join(d, "disk.qcow2"), f"{disk}G"],
                   check=True, stdout=subprocess.DEVNULL)
    m = {"os": os_name, "iso": os.path.abspath(iso), "memory_mb": memory * 1024, "cpus": cpus, "width": width,
         "height": height, "layout": layout, "offline": offline or sandbox, **({"sandbox": True} if sandbox else {}),
         "max_cpus": top[0], "max_memory_mb": top[1] * 1024, "disk_gb": disk,
         **({"balloon": True} if p["family"] == "linux" else {})}
    if p["family"] == "windows":
        # 12 characters: within XP's 14-character limit, and the suffix meets Server's complexity policy
        # (an unmet policy would stop the unattended install at a password prompt).
        m.update(user="Administrator" if p["unattend"] == "sif" else "user",
                 password=secrets.token_urlsafe(6) + "-Aa1")
        write_answers(d, m, key, ui_language)
    with open(os.path.join(d, "machine.json"), "w") as f:
        json.dump(m, f)
    out = {"created": name, "os": os_name, "path": d}
    if p["family"] == "windows":
        out.update(user=m["user"], password=m["password"], note="Windows installs unattended on first start (10-40 min)")
    print(json.dumps(out))


def write_answers(d, m, key, ui_language):
    """The answers drive of a Windows VM (answer file, guest tools), from machine.json's os, layout, user, password."""
    p = PROFILES[m["os"]]
    os.makedirs(os.path.join(d, "answers"), exist_ok=True)
    if p["unattend"] == "sif":
        content, filename = winnt_sif(m["layout"], m["password"], key), "winnt.sif"
    else:
        if not re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}", ui_language or ""):
            raise SystemExit("--ui-language must look like en-US or fr-FR (the language of the ISO)")
        content, filename = autounattend(p, m["layout"], m["user"], m["password"], key, ui_language), "autounattend.xml"
    with open(os.path.join(d, "answers", filename), "w", encoding="utf-8", newline="") as f:
        f.write(content)
    tools = guest_tools() if m["os"] in GUEST_TOOLS_OS else None
    if tools:  # installed at first logon from the answers drive (autounattend: GUEST_TOOLS_CMD)
        shutil.copyfile(tools, os.path.join(d, "answers", "virtio-win-guest-tools.exe"))
        m["balloon"] = True  # the tools include the balloon driver


def start(name, window=False):
    with quota_lock():
        m = load(name)
        if state(name) == "stopped":
            if installing(vm_dir(name), m):  # Setup is CPU-bound: use the VM's ceiling until the first clean stop
                m["install_cpus"] = max(m["cpus"], limits(m)[0])
                try:
                    quota_check({"cpus": m["install_cpus"], "memory_gb": vm_ram_mb(m) / 1024, "vms": 1})
                except SystemExit:
                    del m["install_cpus"]
                save(name, m)
            quota_check({"cpus": run_cpus(name, m), "memory_gb": vm_ram_mb(m) / 1024, "vms": 1})
        _start(name, window)


def _start(name, window=False):
    d, m = vm_dir(name), load(name)
    if state(name) != "stopped":
        raise SystemExit(f"{name} is already running")
    if len(os.path.join(d, "qmp-view.sock").encode()) >= 108:  # sun_path limit
        raise SystemExit(f"VM folder path too long for its sockets: {d}; shorten the name or set TERMINAL_VM_HOME")
    vnc_port = None
    if WINDOWS:
        # QEMU takes a display number (port - 5900); Windows' ephemeral range can start at 1024
        vnc_port = next(p for p in iter(lambda: free_ports(1)[0], None) if p >= 5900)
        with open(os.path.join(d, "ports.json"), "w") as f:
            json.dump({"vnc": vnc_port}, f)
    for k in ("qmp", "qmp-view", "qga", "vnc"):  # stale sockets from a crashed VM
        if os.path.exists(os.path.join(d, k + ".sock")):
            os.unlink(os.path.join(d, k + ".sock"))
    for f in os.listdir(d):  # raw frames left by viewers that were killed (2-10 MB each)
        if f.startswith("view") and f.endswith(".ppm"):
            os.remove(os.path.join(d, f))
    log = open(os.path.join(d, "qemu.log"), "ab")
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if WINDOWS
              else {"start_new_session": True})
    for net in m.get("networks", {}):  # a second switch for the same network exits at once
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_switch", net], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=log, **detach)
    subprocess.Popen([qemu_bin("qemu-system-x86_64")] + qemu_args(name, m, window, vnc_port), stdout=log, stderr=log, **detach)
    for _ in range(100):
        if state(name) != "stopped":
            if m.get("balloon"):  # the driver reads the target when it loads
                q = qmp(name)
                try:
                    q.cmd("balloon", value=m["memory_mb"] * 2 ** 20)
                finally:
                    q.close()
            if WINDOWS:  # VNC password: 8 characters is the protocol's maximum
                pw = secrets.token_urlsafe(6)
                q = qmp(name)
                try:
                    q.cmd("change-vnc-password", password=pw)
                finally:
                    q.close()
                with open(os.path.join(d, "vnc.pass"), "w") as f:
                    f.write(pw)
            print(json.dumps({"started": name})); return
        time.sleep(0.1)
    raise SystemExit(f"QEMU did not start; see {os.path.join(d, 'qemu.log')}")


def stop(name, force=False, timeout=60):
    if state(name) == "stopped":  # a stale qemu.pid may name an unrelated process now
        print(json.dumps({"stopped": name, "already": True})); return
    if force:
        with open(os.path.join(vm_dir(name), "qemu.pid")) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)  # can lose guest work; TerminateProcess on Windows
        print(json.dumps({"stopped": name, "forced": True})); return
    q = qmp(name)
    q.cmd("system_powerdown")
    q.close()
    deadline = time.monotonic() + timeout  # a wall-clock bound: each state() poll can itself wait for QMP
    while time.monotonic() < deadline:  # QEMU exits after the guest powers off; os.kill(pid, 0) would kill on Windows
        if state(name) == "stopped":
            m = load(name)
            if installing(vm_dir(name), m):
                m["installed"] = True; m.pop("install_cpus", None); save(name, m)
            print(json.dumps({"stopped": name})); return
        time.sleep(0.5)
    locked = (" A locked Windows session ignores it too: unlock (key ctrl+alt+delete, type the password from "
              "machine.json, key enter), then stop again." if load(name).get("os", "linux") != "linux" else "")
    print(f"{name}: guest ignored the shutdown request for {timeout}s (no ACPI handling, or a confirmation "
          f"dialog is open); take a screenshot, or use stop --force.{locked}", file=sys.stderr)
    sys.exit(1)


def pause(name, resume=False):
    q = qmp(name)
    q.cmd("cont" if resume else "stop")
    q.close()
    print(json.dumps({"name": name, "status": "running" if resume else "paused"}))


# ---------- macOS guests: terminal-mac ----------
# macOS runs only on Apple hardware (Apple's licence, and Apple Virtualization needs Apple silicon), so macOS guests
# belong to terminal-mac, the Swift engine. terminal-vm is the single entry point: on a Mac it hands macOS VMs over
# to terminal-mac with the arguments unchanged (same command names, same MCP tools).
MAC = sys.platform == "darwin"
MAC_HOME = os.environ.get("TERMINAL_MAC_HOME") or os.path.expanduser("~/Library/Application Support/terminal-mac")
NAMED = ("start", "stop", "status", "show", "hide", "pause", "resume", "view", "display", "action", "mcp")


def mac_bin():
    return os.environ.get("TERMINAL_MAC_BIN") or shutil.which("terminal-mac")


def macos_handoff(argv):
    """The arguments to give terminal-mac when they concern a macOS guest, else None."""
    if not argv:
        return None
    cmd, rest = argv[0], argv[1:]
    if "--os" in rest and rest[rest.index("--os") + 1:][:1] == ["macos"]:
        if not MAC:
            raise SystemExit("macOS guests run only on a Mac with Apple silicon (Apple's licence and Apple "
                             "Virtualization); on this host use --os linux or a Windows version")
        i = rest.index("--os")
        return [cmd] + rest[:i] + rest[i + 2:]  # terminal-mac makes macOS VMs only: no --os
    if not MAC:
        return None
    if cmd in ("up", "create", "image") and "--os" not in rest:
        return argv  # a Mac's own guest by default, as terminal-mac always did (x86 guests would be emulated)
    if cmd not in NAMED:
        return None
    name = next((a for a in rest if not a.startswith("-")), "default")
    mine = os.path.isfile(os.path.join(STORE, name, "machine.json"))
    return argv if not mine and os.path.isdir(os.path.join(MAC_HOME, "machines", name)) else None


def run_mac(args):
    exe = mac_bin()
    if not exe:
        raise SystemExit("this is a macOS VM: install terminal-mac (https://github.com/govijr/terminal-mac) "
                         "or set TERMINAL_MAC_BIN")
    return subprocess.run([exe] + args).returncode  # stdio inherited: the viewer and the MCP server work through it


def mac_machines():
    """terminal-mac's VMs in ls's shape (empty off a Mac or without terminal-mac)."""
    exe = mac_bin() if MAC else None
    if not exe:
        return []
    r = subprocess.run([exe, "ls"], capture_output=True, text=True)
    try:
        machines = json.loads(r.stdout) if r.returncode == 0 else []
    except ValueError:
        return []
    return [{"name": m["name"], "os": "macos", "engine": "terminal-mac", "cpus": m.get("cpu"),
             "memory_gb": m.get("memoryGiB"), "display": f"{m.get('width')}x{m.get('height')}"} for m in machines]


# ---------- networks ----------
# Isolated networks between VMs, no administrator rights: each network is a small learning switch (net_switch, UDP
# on the loopback interface) and each NIC a QEMU dgram socket plugged into it. Nothing reaches the LAN or the
# internet; a local process could send to or read from the switch port. No DHCP: give guests static addresses (net ls suggests one per
# VM), or run a DHCP server in one of them, as in a real network.
NET_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")


def net_file(net):
    if not NET_NAME.fullmatch(net or ""):
        raise SystemExit("network names: lowercase letters, digits and -, up to 31 characters")
    return os.path.join(STORE, "networks", net + ".json")


def net_load(net):
    try:
        with open(net_file(net)) as f:
            return json.load(f)
    except OSError:
        raise SystemExit(f"no network named {net!r}; create it with: terminal-vm net create {net}") from None


def networks():
    d = os.path.join(STORE, "networks")
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []


def net_members(net):
    return [(n, load(n)["networks"][net]) for n in machines() if net in load(n).get("networks", {})]


def net_create(net):
    path = net_file(net)
    with quota_lock():  # the host-wide lock also makes the index allocation atomic
        if os.path.exists(path):
            raise SystemExit(f"network {net!r} already exists")
        used = {net_load(n)["index"] for n in networks()}
        idx = next((i for i in range(1, 251) if i not in used), None)
        if idx is None:
            raise SystemExit("250 networks at most")
        cfg = {"name": net, "index": idx, "port": 40000 + idx, "subnet": f"10.77.{idx}.0/24"}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f)
    return cfg


def net_rm(net):
    net_load(net)
    if net_members(net):
        raise SystemExit(f"network {net!r} still has VMs: " + ", ".join(n for n, _ in net_members(net))
                         + f"; detach them first: terminal-vm net detach VM {net}")
    os.unlink(net_file(net))
    return {"removed": net}


def net_ls():
    return [dict(net_load(n), vms=[{"vm": v, **a} for v, a in net_members(n)]) for n in networks()]


def net_attach(name, net, attach=True):
    """Plug a stopped VM into a network (a NIC with a stable MAC and a suggested address), or unplug it."""
    cfg, m = net_load(net), load(name)
    if state(name) != "stopped":
        raise SystemExit(f"{name} is running; network cards change on a stopped VM: terminal-vm stop {name}")
    nets = m.setdefault("networks", {})
    if not attach:
        if nets.pop(net, None) is None:
            raise SystemExit(f"{name} is not on {net}")
        save(name, m)
        return {"vm": name, "detached": net}
    ports = {a.get("port") for n in machines() for a in load(n).get("networks", {}).values()}
    port = next(p for p in range(41000, 65536) if p not in ports)
    if net in nets:
        nets[net].setdefault("port", port)  # attachments made before the switch had none
        save(name, m)
        return {"vm": name, "network": net, **nets[net]}
    taken = {int(a["ip"].rsplit(".", 1)[1]) for _, a in net_members(net)}
    host = next(i for i in range(10, 255) if i not in taken)
    mac = "52:54:00:" + ":".join(f"{b:02x}" for b in hashlib.sha256(f"{name}/{net}".encode()).digest()[:3])
    nets[net] = {"mac": mac, "ip": f"10.77.{cfg['index']}.{host}", "port": port}
    save(name, m)
    return {"vm": name, "network": net, **nets[net], "netmask": "255.255.255.0",
            "note": "no DHCP on this network: set this address in the guest"}


def set_internet(name, value=None):
    """Show or change NAT internet access. For a sandboxed VM, turning it on is the user's approval."""
    m = load(name)
    if value:
        if state(name) != "stopped":
            raise SystemExit(f"{name} is running; stop it first: terminal-vm stop {name}")
        on = value == "on"
        m["offline"] = not on
        m.pop("internet", None)
        if on and m.get("sandbox"):
            m["internet"] = True  # approved by the user for this sandboxed VM
        save(name, m)
    sandbox_blocks = m.get("sandbox") and not m.get("internet")
    return {"vm": name, "internet": not m.get("offline") and not sandbox_blocks}


def net_args(m, nic):
    args = []
    for net, a in sorted(m.get("networks", {}).items()):
        cfg = net_load(net)
        if "port" not in a or "port" not in cfg:
            raise SystemExit(f"network {net!r} predates the switch: recreate it, or run net attach again for this VM")
        args += ["-netdev", f"dgram,id=n-{net},local.type=inet,local.host=127.0.0.1,local.port={a['port']},"
                 f"remote.type=inet,remote.host=127.0.0.1,remote.port={cfg['port']}",
                 "-device", f"{nic},netdev=n-{net},mac={a['mac']}"]
    return args


def net_switch(net, sock=None, alive=None):
    """Run network NET's switch: forward each Ethernet frame to the port that sent its destination MAC, flood the
    rest. Exits when the port is taken (a switch already runs) or once none of the network's VMs runs."""
    port = net_load(net)["port"]
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return
        sock.settimeout(30)
    alive = alive or (lambda: any(state(n) != "stopped" for n, _ in net_members(net)))
    macs, peers, idle = {}, set(), 0
    while idle < 2:  # two checks in a row: the VM that started this switch may still be booting QEMU
        try:
            frame, src = sock.recvfrom(65536)
        except socket.timeout:
            idle = 0 if alive() else idle + 1
            continue
        except ConnectionResetError:  # Windows reports an earlier send to a VM that has stopped
            continue
        if len(frame) < 14:
            continue
        # shortcut: peers are learned from what they send and never forgotten; a VM that stays silent gets no
        # broadcasts until it sends one frame (every OS does at boot). Ports are fixed per NIC, so the set stays small.
        peers.add(src)
        macs[frame[6:12]] = src
        dst = macs.get(frame[0:6]) if not frame[0] & 1 else None  # group bit set: broadcast or multicast
        for p in [dst] if dst else peers - {src}:
            try:
                sock.sendto(frame, p)
            except OSError:
                pass


# ---------- monitoring ----------

def _proc_times(pid):
    """(CPU seconds used, resident bytes) of a host process, or None if it is gone."""
    try:
        if WINDOWS:
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wintypes.HANDLE
            h = k.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            if not h:
                return None
            try:
                t = [wintypes.FILETIME() for _ in range(4)]
                k.GetProcessTimes(h, *[ctypes.byref(x) for x in t])
                cpu = sum((x.dwHighDateTime << 32 | x.dwLowDateTime) for x in t[2:]) / 1e7

                class Mem(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + \
                               [(n, ctypes.c_size_t) for n in ("peak_ws", "ws", "qpp", "qp", "qnpp", "qnp", "pf", "ppf")]
                mem = Mem(); mem.cb = ctypes.sizeof(mem)
                ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(mem), mem.cb)
                return cpu, mem.ws
            finally:
                k.CloseHandle(h)
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        with open(f"/proc/{pid}/statm") as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"), rss
    except (OSError, ValueError, IndexError):
        return None


def top(sample=0.5):
    """Real host use of each running VM (QEMU's CPU % of one core, resident memory) next to what it is allotted,
    and the quota. Read-only: for people and agents to see where they stand."""
    running = []
    for n in machines():
        if state(n) == "stopped":
            continue
        try:
            with open(os.path.join(vm_dir(n), "qemu.pid")) as f:
                running.append((n, int(f.read().strip())))
        except (OSError, ValueError):
            pass
    before = {n: _proc_times(pid) for n, pid in running}
    time.sleep(sample if running else 0)
    vms = []
    for n, pid in running:
        m, a, b = load(n), before[n], _proc_times(pid)
        top_cpus, top_gb = limits(m)
        vms.append({"name": n, "os": m.get("os", "linux"), "cpus": m["cpus"], "max_cpus": top_cpus,
                    "memory_gb": m["memory_mb"] // 1024, "max_memory_gb": top_gb,
                    **({"cpu_percent": round((b[0] - a[0]) / sample * 100), "host_memory_gb": round(b[1] / 2 ** 30, 2)}
                       if a and b else {}),
                    **({"sandbox": True} if m.get("sandbox") else {})})
    return {"vms": vms, "quota": quota()}


# ---------- quota ----------
# One ceiling for everything this host runs, set by the user (`quota`), by default from the host. Checked under a
# lock at create, clone, start and resources, so no sequence of commands, human or agent, gets past it. Memory counts
# each running VM's ceiling (QEMU's -m): a guest may refuse to give memory back through the balloon.
QUOTA_KEYS = ("cpus", "memory_gb", "disk_gb", "vms")


def vm_ram_mb(m):
    """Memory QEMU gets for this VM: its ceiling with a balloon, else its allocation."""
    return max(limits(m)[1] * 1024, m["memory_mb"]) if m.get("balloon") else m["memory_mb"]


def vm_disk_gb(name, m):
    """Virtual disk size, what the guest can fill (stored at create; read once for older VMs)."""
    if "disk_gb" not in m:
        r = subprocess.run([qemu_bin("qemu-img"), "info", "-U", "--output=json", os.path.join(vm_dir(name), "disk.qcow2")],
                           capture_output=True, text=True)
        try:
            m["disk_gb"] = -(-json.loads(r.stdout)["virtual-size"] // 2 ** 30)
        except (ValueError, KeyError):
            return 0
        save(name, m)
    return m["disk_gb"]


def machines():
    return sorted(n for n in os.listdir(STORE) if os.path.isfile(os.path.join(STORE, n, "machine.json"))) \
        if os.path.isdir(STORE) else []


def quota_usage():
    use = {"cpus": 0, "memory_gb": 0, "disk_gb": 0, "vms": 0}
    for n in machines():
        m = load(n)
        use["disk_gb"] += vm_disk_gb(n, m)
        if state(n) != "stopped":
            use["cpus"] += run_cpus(n, m); use["memory_gb"] += vm_ram_mb(m) / 1024; use["vms"] += 1
    return use


def in_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def host_free_bytes(path):
    """Free space VM disks can grow into. Under WSL the store sits in a virtual disk that df reports at its
    maximum size while it really grows inside a Windows drive: count that drive too (C: unless it is missing)."""
    free = shutil.disk_usage(path).free
    if in_wsl() and os.path.isdir("/mnt/c"):  # shortcut: assumes the distribution's disk is on C:, the default
        free = min(free, shutil.disk_usage("/mnt/c").free)
    return free


def quota_limits(use=None):
    """The user's quota, defaults from the host for what was never set: cores - 1, 75% of RAM, half of the disk
    space VMs can have (free now plus what their disk files already take), 8 running VMs. Disks count at their
    virtual size (a guest can fill it), so on a small drive the default leaves little room for new VMs."""
    set_ = _quota_file()
    top = STORE if os.path.isdir(STORE) else os.path.dirname(STORE)
    held = sum(os.path.getsize(f) for f in (os.path.join(STORE, n, "disk.qcow2") for n in machines()) if os.path.isfile(f))
    default = {"cpus": max(1, os.cpu_count() - 1), "memory_gb": max(1, host_memory_gb() * 3 // 4),
               "disk_gb": (host_free_bytes(top) + held) // 2 ** 31, "vms": 8}
    return {k: set_.get(k, default[k]) for k in QUOTA_KEYS}, {k: k in set_ for k in QUOTA_KEYS}


class quota_lock:
    """Cross-process lock (an atomic mkdir), so two commands cannot both pass the check and then both start."""
    def __enter__(self):
        os.makedirs(STORE, exist_ok=True)
        self.path = os.path.join(STORE, ".quota.lock")
        for _ in range(300):
            try:
                os.mkdir(self.path); return self
            except FileExistsError:
                if time.time() - os.path.getmtime(self.path) > 120:  # left by a crashed command
                    os.rmdir(self.path)
                time.sleep(0.1)
        raise SystemExit("another terminal-vm command holds the quota lock; try again")

    def __exit__(self, *exc):
        os.rmdir(self.path)


def quota_check(add):
    """Refuse when adding `add` (cpus, memory_gb, disk_gb, vms) to current use would pass the quota."""
    use = quota_usage()
    lim = quota_limits(use)[0]
    over = [f"{k}: {use[k]:g} used + {add[k]:g} > {lim[k]}" for k in QUOTA_KEYS if add.get(k) and use[k] + add[k] > lim[k]]
    if over:
        raise SystemExit("quota exceeded (" + "; ".join(over) + "). Free resources (stop or shrink VMs), "
                         "or the user raises the quota: terminal-vm quota --cpus/--memory/--disk/--vms")


def quota(set_=None):
    """Show the quota and its use, or (user only) change it."""
    if set_:
        if any(v is not None and v < 1 for v in set_.values()):
            raise SystemExit("quota values must be at least 1")
        with quota_lock():
            stored = _quota_file()
            stored.update({k: v for k, v in set_.items() if v is not None})
            with open(os.path.join(STORE, "quota.json"), "w") as f:
                json.dump(stored, f)
    use = quota_usage()
    lim, chosen = quota_limits(use)
    return {k: {"limit": lim[k], "used": round(use[k], 1), "free": round(lim[k] - use[k], 1),
                "source": "user" if chosen[k] else "host default"} for k in QUOTA_KEYS}


def ls():
    out = []
    if os.path.isdir(STORE):
        for n in sorted(os.listdir(STORE)):
            if os.path.isfile(os.path.join(STORE, n, "machine.json")):
                m = load(n)
                out.append({"name": n, "os": m.get("os", "linux"), "status": state(n), "cpus": m["cpus"], "memory_gb": m["memory_mb"] // 1024,
                            "display": f"{m.get('width', 1280)}x{m.get('height', 800)}", "layout": m.get("layout", "us"),
                            **({"base": m["base"]} if m.get("base") else {}), **({"sandbox": True} if m.get("sandbox") else {})})
    print(json.dumps(out + mac_machines(), indent=1))


def _stopped(name):
    load(name)
    busy = state(name) != "stopped"
    disk = os.path.join(vm_dir(name), "disk.qcow2")
    if not busy and os.path.exists(disk):  # QMP unreachable is not proof: a running QEMU also holds the image lock
        busy = subprocess.run([qemu_bin("qemu-img"), "info", disk], capture_output=True).returncode != 0
    if busy:
        raise SystemExit(f"{name} is running; stop it first: terminal-vm stop {name}")


def _snapshots(disk):
    r = subprocess.run([qemu_bin("qemu-img"), "snapshot", "-l", disk], capture_output=True, text=True)
    return [t[1] for t in (line.split() for line in r.stdout.splitlines()) if len(t) > 2 and t[0].isdigit()]


def _backing(disk):
    r = subprocess.run([qemu_bin("qemu-img"), "info", "-U", "--output=json", disk], capture_output=True, text=True)
    try:
        return json.loads(r.stdout).get("backing-filename")
    except ValueError:
        return None


def _untouched(disk):
    """An overlay holding no data of its own (every cluster comes from its backing file)."""
    r = subprocess.run([qemu_bin("qemu-img"), "map", "-U", "--output=json", disk], capture_output=True, text=True)
    try:
        return r.returncode == 0 and not any(e["depth"] == 0 and e["data"] for e in json.loads(r.stdout))
    except (ValueError, KeyError):
        return False


def _gc_bases():
    """Delete the base images no VM disk (or other base) is built on any more."""
    bases = os.path.join(STORE, ".bases")
    while os.path.isdir(bases):
        files = {os.path.normcase(os.path.join(bases, f)) for f in os.listdir(bases)}
        disks = [os.path.join(STORE, n, "disk.qcow2") for n in os.listdir(STORE)] + list(files)
        used = {os.path.normcase(os.path.abspath(b)) for b in map(_backing, filter(os.path.isfile, disks)) if b}
        if not files - used:
            return
        for f in files - used:
            os.chmod(f, 0o600)
            os.remove(f)


def clone(src, new):
    """New VM whose disk is a copy-on-write overlay (instant, small). SRC's disk becomes a read-only base image
    under STORE/.bases and SRC an overlay on it too, so both keep running and changing on their own; the base
    goes when its last VM is removed or reset."""
    _stopped(src)
    d = vm_dir(new)
    if os.path.exists(os.path.join(d, "machine.json")):
        raise SystemExit(f"VM {new!r} already exists in {d}")
    disk = os.path.join(vm_dir(src), "disk.qcow2")
    snaps = _snapshots(disk)
    if snaps:  # they would stay inside the base, out of reach of SRC's overlay
        raise SystemExit(f"{src} has snapshots ({', '.join(snaps)}): load the one to clone from, then remove them "
                         f"(terminal-vm snapshot {src} rm TAG) and clone again")
    m = load(src)
    with quota_lock():
        quota_check({"disk_gb": vm_disk_gb(src, m)})  # the overlay can grow to the base's size
        os.makedirs(d, mode=0o700, exist_ok=True)
        save(new, dict(m, base=src))
    bases, base = os.path.join(STORE, ".bases"), _backing(disk)
    if base and os.path.dirname(os.path.abspath(base)) == os.path.abspath(bases) and _untouched(disk):
        targets = [os.path.join(d, "disk.qcow2")]  # SRC wrote nothing since its last clone: share that base
    else:  # a new base layer
        os.makedirs(bases, exist_ok=True)
        base = os.path.join(bases, f"{src}-{secrets.token_hex(4)}.qcow2")
        targets = [disk, os.path.join(d, "disk.qcow2")]
        os.replace(disk, base)
    try:
        for target in targets:
            subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", "-F", "qcow2", "-b", base, target],
                           check=True, stdout=subprocess.DEVNULL)
    except BaseException:
        if disk in targets:
            os.replace(base, disk)  # SRC as it was; the half-made clone goes
        shutil.rmtree(d, ignore_errors=True)
        raise
    os.chmod(base, 0o444)  # QEMU only reads it; nothing should write it while overlays exist
    m.update(base=src, installed=True)  # no answers drive in the clone: it boots the installed disk
    m.pop("networks", None)  # each NIC's MAC is per VM: attach the clone again
    save(new, m)
    print(json.dumps({"cloned": src, "to": new}))


def rm(name):
    _stopped(name)
    shutil.rmtree(vm_dir(name))  # its network memberships go with its machine.json
    _gc_bases()
    print(json.dumps({"removed": name}))


def reset(name):
    """Blank disk of the same size, so the next start installs again (Windows answers rewritten, account kept).
    Deletes the disk's snapshots too."""
    _stopped(name)
    disk = os.path.join(vm_dir(name), "disk.qcow2")
    info = subprocess.run([qemu_bin("qemu-img"), "info", "--output=json", disk], check=True, capture_output=True, text=True)
    subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", disk + ".new", str(json.loads(info.stdout)["virtual-size"])],
                   check=True, stdout=subprocess.DEVNULL)
    os.replace(disk + ".new", disk)  # the old disk survives a failed create
    _gc_bases()
    m = load(name)
    m.pop("installed", None)
    answers = os.path.join(vm_dir(name), "answers")
    if PROFILES[m.get("os", "linux")]["family"] == "windows" and os.path.isdir(answers):
        # rewrite the answers with this version (fixes and guest tools added since creation), same
        # account, ISO language and key: those two are read back from the previous answer file
        old = "".join(open(os.path.join(answers, f), encoding="utf-8").read() for f in os.listdir(answers)
                      if f in ("autounattend.xml", "winnt.sif"))
        ui = re.search(r"<UILanguage>([^<]+)</UILanguage>", old)
        key = re.search(r"<ProductKey><Key>([^<]+)</Key>|^ProductKey=(\S+)", old, re.M)
        write_answers(vm_dir(name), m, key and (key.group(1) or key.group(2)), ui.group(1) if ui else "en-US")
    save(name, m)
    print(json.dumps({"reset": name}))


def snapshot(name, cmd, tag=None):
    """qcow2 internal snapshots. Running VM: whole machine, memory included (savevm, pauses it a few
    seconds). Stopped VM: disk only (qemu-img); loading one then boots from that disk state."""
    load(name)
    if cmd != "ls" and not re.fullmatch(r"[\w.-]{1,64}", tag or ""):
        raise SystemExit(f"snapshot {cmd} needs a TAG (letters, digits, . _ -)")
    if cmd != "ls":
        if load(name).get("sandbox") and state(name) != "stopped":
            raise SystemExit(f"{name} is sandboxed: its disk changes are discarded at stop, so stop it first "
                             "(a stopped VM's snapshot is the state each sandboxed run starts from)")
    if state(name) == "stopped":
        flag = {"save": "-c", "load": "-a", "rm": "-d", "ls": "-l"}[cmd]
        r = subprocess.run([qemu_bin("qemu-img"), "snapshot", flag, *([tag] if tag else []), os.path.join(vm_dir(name), "disk.qcow2")],
                           capture_output=True, text=True)
        out, err = r.stdout, (r.stderr or f"qemu-img snapshot {flag} failed") if r.returncode else ""
    else:
        q = qmp(name)
        try:
            if cmd == "rm" and f" {tag} " not in q.cmd("human-monitor-command", **{"command-line": "info snapshots"}):
                raise SystemExit(f"no snapshot {tag!r}")  # delvm says nothing about a missing tag
            hmp = {"save": "savevm", "load": "loadvm", "rm": "delvm", "ls": "info snapshots"}[cmd]
            out = q.cmd("human-monitor-command", **{"command-line": f"{hmp} {tag}" if tag else hmp}) or ""
        finally:
            q.close()
        err = out if cmd != "ls" and "rror" in out else ""  # HMP reports failures as text
    if err:
        raise SystemExit(err.strip())
    if cmd == "ls":  # rows: ID TAG VM-SIZE DATE ...; ID is "--" in `info snapshots`
        tags = [t[1] for t in (line.split() for line in out.splitlines()) if len(t) > 2 and (t[0].isdigit() or t[0] == "--")]
        print(json.dumps({"name": name, "snapshots": list(dict.fromkeys(tags))})); return
    print(json.dumps({"name": name, "snapshot": tag, {"save": "saved", "load": "loaded", "rm": "removed"}[cmd]: True}))


def display(name, width, height):
    """Screen size used at the next boot (the guest may still pick another mode)."""
    _stopped(name)
    m = load(name)
    if not (640 <= width <= 7680 and 480 <= height <= 4320):
        raise SystemExit("invalid size: 640-7680 x 480-4320")
    m.update(width=width, height=height)
    save(name, m)
    print(json.dumps({"name": name, "display": f"{width}x{height}", "applies": "next start"}))


def save(name, m):
    path = os.path.join(vm_dir(name), "machine.json")
    with open(path + ".new", "w") as f:
        json.dump(m, f)
    os.replace(path + ".new", path)  # a crash mid-write never leaves a truncated machine.json


def set_layout(name, layout=None):
    """Show or change the keyboard layout used by type/key. Host side only, so allowed while running;
    it must match the layout active in the guest (Windows: Win+Space switches)."""
    m = load(name)
    if layout:
        if layout not in LAYOUTS:
            raise SystemExit(f"unknown layout {layout!r}; choose from {', '.join(LAYOUTS)}")
        m["layout"] = layout
        save(name, m)
    print(json.dumps({"name": name, "layout": m.get("layout", "us")}))


def _online_vcpus(name):
    """Bring every guest CPU online through qemu-guest-agent; False when no agent answers."""
    try:
        g = QGA(name, timeout=3)
        try:
            off = [{"logical-id": c["logical-id"], "online": True} for c in g.call("guest-get-vcpus") if not c["online"]]
            if off:
                g.call("guest-set-vcpus", vcpus=off)
            return True
        finally:
            g.sock.close()
    except (OSError, RuntimeError, ValueError):
        return False


def set_ceiling(name, max_cpus=None, max_memory=None):
    """Change a stopped VM's ceiling, within the host and the quota (it then counts at the next start)."""
    m = load(name)
    if state(name) != "stopped":
        raise SystemExit(f"{name} is running; ceilings change on a stopped VM: terminal-vm stop {name}")
    q = quota_limits()[0]
    for what, v, host, lim in (("cpus", max_cpus, os.cpu_count(), q["cpus"]),
                               ("memory", max_memory, host_memory_gb(), q["memory_gb"])):
        if v is not None and not 1 <= v <= min(host, lim):
            raise SystemExit(f"--max-{what} must be 1..{min(host, lim)} (host and quota)")
    if max_cpus:
        m["max_cpus"] = max_cpus; m["cpus"] = min(m["cpus"], max_cpus)
    if max_memory:
        m["max_memory_mb"] = max_memory * 1024; m["memory_mb"] = min(m["memory_mb"], max_memory * 1024)
    save(name, m)
    return _resources(name)


def resources(name, cpus=None, memory=None, max_cpus=None, max_memory=None):
    """See _resources. More CPUs on a running VM go through the quota, under its lock until they are added."""
    if max_cpus or max_memory:
        set_ceiling(name, max_cpus, max_memory)
    if cpus is not None and state(name) != "stopped" and cpus > load(name)["cpus"]:
        with quota_lock():
            quota_check({"cpus": cpus - load(name)["cpus"]})  # memory moves under the ceiling, already counted
            return _resources(name, cpus, memory)
    return _resources(name, cpus, memory)


def _resources(name, cpus=None, memory=None):
    """Show or change CPUs and memory (GiB) within the VM's ceiling. Running VM: memory changes now through the
    balloon; CPUs are added now on Linux guests. Anything else (fewer CPUs, Windows CPUs, no balloon) is saved for
    the next start, and the reply says which."""
    m = load(name)
    top_cpus, top_gb = limits(m)
    for what, v, top in (("cpus", cpus, top_cpus), ("memory", memory, top_gb)):
        if v is not None and not 1 <= v <= top:
            raise SystemExit(f"{what} must be 1..{top} for {name}, its ceiling (resources {name} --max-{what} N "
                             "on the stopped VM raises it, within the quota)")
    running, applied = state(name) != "stopped", {}
    q = qmp(name) if running and (cpus or memory) else None
    try:
        if memory is not None:
            if q and m.get("balloon") and memory * 1024 <= max(top_gb * 1024, m["memory_mb"]):
                q.cmd("balloon", value=memory * 2 ** 30); applied["memory"] = "now"
            else:
                applied["memory"] = "next start" if running else "saved"
            m["memory_mb"] = memory * 1024
        if cpus is not None:
            if q and hot_cpus(m):
                slots = q.cmd("query-hotpluggable-cpus")
                online = [c for c in slots if c.get("qom-path")]
                free = [c for c in slots if not c.get("qom-path")]
                for c in free[:max(0, cpus - len(online))]:
                    props = {k: v for k, v in c["props"].items() if k.endswith("-id")}
                    q.cmd("device_add", driver=c["type"], id="cpu" + "-".join(str(v) for v in props.values()), **props)
                applied["cpus"] = "now" if cpus >= len(online) else "next start"  # removal needs a restart
                if applied["cpus"] == "now" and not _online_vcpus(name):  # most distributions leave new CPUs offline
                    applied["cpus"] = ("added, offline until the guest brings them up (qemu-guest-agent does it; "
                                       "or as root: echo 1 > /sys/devices/system/cpu/cpuN/online)")
            else:
                applied["cpus"] = "next start" if running else "saved"
            m["cpus"] = cpus
        out = {"name": name, "cpus": m["cpus"], "memory_gb": m["memory_mb"] // 1024,
               "max_cpus": top_cpus, "max_memory_gb": top_gb, "live_memory": bool(m.get("balloon")),
               "live_cpus": hot_cpus(m)}
        if running and m.get("balloon"):
            q2 = q or qmp(name)
            try:
                out["guest_memory_gb"] = round(q2.cmd("query-balloon")["actual"] / 2 ** 30, 1)
            finally:
                if q2 is not q:
                    q2.close()
    finally:
        if q:
            q.close()
    if cpus is not None or memory is not None:
        save(name, m)
        out["applied"] = applied
    return out


def set_sandbox(name, value=None):
    """Show or change sandbox mode (stopped VM: it shapes the QEMU command line at the next start)."""
    m = load(name)
    if value:
        if state(name) != "stopped":
            raise SystemExit(f"{name} is running; stop it first: terminal-vm stop {name}")
        m["sandbox"] = value == "on"
        m.pop("internet", None)  # an earlier internet approval does not carry over
        if not m["sandbox"]:
            m.pop("sandbox")
        save(name, m)
    print(json.dumps({"name": name, "sandbox": bool(m.get("sandbox")),
                      **({"note": "no network, no guest agent or file copy, disk changes discarded at stop, "
                                  "MCP actions logged to actions.log"} if m.get("sandbox") else {})}))


def transfer(name, direction, src, dest):
    """Copy a file to (push) or from (pull) the guest through qemu-guest-agent."""
    if load(name).get("sandbox"):
        raise SystemExit(f"{name} is sandboxed: no file copy in or out (terminal-vm sandbox {name} off to allow it)")
    try:
        g = QGA(name)
    except OSError:
        raise SystemExit("no guest agent answering: install qemu-guest-agent in the guest "
                         "(Windows: virtio-win guest tools)") from None
    g.sock.settimeout(60)  # large files: QGA(name) connects with a short timeout
    try:
        if direction == "push":
            with open(src, "rb") as f:
                data = f.read()
            g.push(dest, data)
        else:
            data = g.pull(src, "rb")
            with open(dest, "wb") as f:
                f.write(data)
    finally:
        g.sock.close()
    print(json.dumps({direction + "ed": src, "to": dest, "bytes": len(data)}))


# ---------- native window (VNC) ----------

VIEWERS = [  # endpoint: unix socket path, or (host, port) on Windows
    ("remote-viewer", lambda e: ["remote-viewer", f"vnc://{e[0]}:{e[1]}" if isinstance(e, tuple) else f"vnc+unix://{e}"]),
    ("vncviewer", lambda e: ["vncviewer", f"{e[0]}::{e[1]}" if isinstance(e, tuple) else e]),  # TigerVNC takes a socket path
]


def show(name, read_only=False):
    d = vm_dir(name)
    if state(name) == "stopped":
        raise SystemExit(f"{name} is not running; run: terminal-vm start {name}")
    for exe, argv in VIEWERS[1:] if read_only else VIEWERS:  # only TigerVNC has a view-only mode
        if shutil.which(exe):
            detach = {"creationflags": subprocess.DETACHED_PROCESS} if WINDOWS else {"start_new_session": True}
            a = argv(endpoint(name, "vnc"))
            a[1:1] = ["-ViewOnly"] if read_only else []
            p = subprocess.Popen(a, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
            with open(os.path.join(d, "viewer.pid"), "w") as f:
                f.write(str(p.pid))
            out = {"shown": name, "viewer": exe}
            if WINDOWS:  # the viewer asks for it
                with open(os.path.join(d, "vnc.pass")) as f:
                    out["vnc_password"] = f.read()
            print(json.dumps(out)); return
    if read_only:
        raise SystemExit("--read-only needs TigerVNC (tigervnc-viewer)")
    raise SystemExit("no VNC viewer found: install TigerVNC (tigervnc-viewer) or virt-viewer, "
                     f"or restart with a window: terminal-vm stop {name}; terminal-vm start {name} --window")


def hide(name):
    path = os.path.join(vm_dir(name), "viewer.pid")
    try:
        with open(path) as f:
            os.kill(int(f.read()), signal.SIGTERM)
    except (OSError, ValueError):  # gone already (Windows raises plain OSError), or an empty file
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
_CELL = re.compile(r"\x1b\[6;(\d+);(\d+)t")  # reply to CSI 16 t: cell height, width in pixels
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
            m = _CELL.match(data, i)
            if m:
                ev.append(("cell", int(m[1]), int(m[2]))); i = m.end(); continue
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


def graphics():
    """Inline image protocol of this terminal, from its environment (no query round-trip): kitty, sixel or None.
    WT_SESSION: Windows Terminal (Sixel since 1.22); WT also passes it into WSL."""
    e = os.environ
    if WINDOWS:  # other terminals reach Windows programs through ConPTY, which drops image sequences (WezTerm tested)
        return "sixel" if "WT_SESSION" in e else None
    if "KITTY_WINDOW_ID" in e or e.get("TERM") == "xterm-kitty" or e.get("TERM_PROGRAM", "").lower() in ("ghostty", "wezterm"):
        return "kitty"
    if "WT_SESSION" in e or e.get("TERM") in ("foot", "foot-extra", "mlterm") or e.get("TERM_PROGRAM") == "mintty":
        return "sixel"
    return None


def term_geometry(cell=(20, 10)):
    """cols, rows, cell width and height in pixels. cell: (height, width) as the terminal answered CSI 16 t,
    used when the tty gives no pixel size (Windows consoles, some terminals)."""
    xp = yp = 0
    try:
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
    except (OSError, NameError):  # NameError: no fcntl on Windows
        cols, rows = shutil.get_terminal_size()
    return cols, rows, (xp / cols if xp else cell[1]), (yp / rows if yp else cell[0])


def fit(w, h, cols, rows, cw, ch):
    """Cells used to show a w x h image in cols x (rows-1) cells, keeping its aspect ratio."""
    scale = min(cols * cw / w, (rows - 1) * ch / h)
    return max(1, int(w * scale / cw)), max(1, int(h * scale / ch))


def screen_pixels(name, q):
    """Raw RGB of the guest screen: screendump's default PPM, so nothing to decode."""
    path = os.path.join(vm_dir(name), f"view-{os.getpid()}.ppm")  # one file per viewer
    q.cmd("screendump", filename=path)
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    w, h = map(int, data.split(maxsplit=3)[1:3])  # "P6\n<w> <h>\n255\n" then the pixels
    return data[-w * h * 3:], (w, h)


# 15-bit colour key, 5 bits per channel (4 bits banded smooth gradients): r << 10 | g << 5 | b, as two bytes
_HI_R, _HI_G = bytes(v >> 3 << 2 for v in range(256)), bytes(v >> 6 for v in range(256))
_LO_G, _LO_B = bytes((v >> 3 & 7) << 5 for v in range(256)), bytes(v >> 3 for v in range(256))
_IS = [bytes(int(i == c) for i in range(256)) for c in range(256)]
_RLE = re.compile(rb"(.)\1{3,}")


def colour_keys(line):
    """RGB bytes -> one 15-bit colour key per pixel, as a memoryview of ints (byte sums never carry)."""
    n, keys = len(line) // 3, bytearray(2 * len(line) // 3)
    r, g, b = line[0::3], line[1::3], line[2::3]
    keys[1::2] = (int.from_bytes(r.translate(_HI_R), "big") + int.from_bytes(g.translate(_HI_G), "big")).to_bytes(n, "big")
    keys[0::2] = (int.from_bytes(g.translate(_LO_G), "big") + int.from_bytes(b.translate(_LO_B), "big")).to_bytes(n, "big")
    return memoryview(keys).cast("H")  # native order is little-endian on x86/ARM: low byte first


def make_palette(rgb, w, h, size=255):
    """The screen's most frequent colours (15-bit keys) and a key -> register table (255: not mapped yet).
    An adaptive palette keeps interface colours nearly exact, where a fixed cube needed a grainy dither."""
    counts = collections.Counter()
    for y in range(0, h, 2):  # every other row ranks the colours just as well, in half the time
        counts.update(colour_keys(rgb[y * w * 3:(y + 1) * w * 3]))
    keys = [k for k, _ in counts.most_common(size)]
    lut = bytearray(b"\xff" * 32768)
    for i, k in enumerate(keys):
        lut[k] = i
    return keys, lut, {}


def _nearest(palette, k):
    """Nearest register to colour key k. Smoothing small text brings thousands of in-between shades per new
    screen: the palette's components are unpacked once (cells[None]) and the search is a flat min over tuples
    (~45 us), which a per-shade lambda made 20 times slower."""
    keys, _, cells = palette
    if None not in cells:
        cells[None] = [(i, q >> 10, q >> 5 & 31, q & 31) for i, q in enumerate(keys)]
    r, g, b = k >> 10, k >> 5 & 31, k & 31
    return min(((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2, i) for i, pr, pg, pb in cells[None])[1]


_HALF = bytes(v >> 1 for v in range(256))


def _blend(a, b):
    """Byte-wise average of two equal-length byte strings (halves summed as big integers: no carry)."""
    return (int.from_bytes(a.translate(_HALF), "big") + int.from_bytes(b.translate(_HALF), "big")).to_bytes(len(a), "big")


def sixel_rows(rgb, w, h, W, H, palette, old=None, prev=None):
    """RGB bytes (w x h) -> W x H rows of palette registers. Shrinking averages each pixel with its right and lower
    neighbours first, so 1-pixel strokes of small text soften instead of vanishing with the skipped columns; growing
    is nearest-neighbour. No per-pixel Python loop except map(): bytes.translate and big-integer sums work on whole
    rows. prev: the rows this returned for the frame old (same sizes): rows whose source lines did not change are
    reused, so a moving pointer costs a few lines, not a frame. Colours missing from the palette get their nearest
    register."""
    lut = palette[1]
    xs, rows, cache, row_len = [x * w // W for x in range(W)], [], {}, w * 3
    taps = 2 if H < h else 1  # source lines behind one output line

    def source(y):
        line = rgb[y * row_len:(y + 1) * row_len]
        return _blend(line, line[3:] + line[-3:]) if W < w else line
    for Y, y in enumerate(y * h // H for y in range(H)):
        span = slice(y * row_len, min(y + taps, h) * row_len)
        if prev and old[span] == rgb[span]:
            rows.append(prev[Y]); continue
        if y not in cache:
            line = source(y) if taps == 1 or y + 1 >= h else _blend(source(y), source(y + 1))
            mv = colour_keys(line)
            vals = mv if W == w else map(mv.__getitem__, xs)
            row = bytes(map(lut.__getitem__, vals))
            if 255 in row:  # colours not in the palette yet: map them, then this row again
                vals = mv if W == w else list(map(mv.__getitem__, xs))
                for k in set(vals):
                    if lut[k] == 255:
                        lut[k] = _nearest(palette, k)
                row = bytes(map(lut.__getitem__, vals))
            cache[y] = row
        rows.append(cache[y])
    return rows


def changed(old, new, gap=20):
    """Boxes (x0, y0, x1, y1) around the bytes that differ between two equal-sized row lists: one per run of
    changed rows (runs closer than gap rows merge), so a pointer that jumped redraws two small boxes, not one big."""
    ys = [y for y, (a, b) in enumerate(zip(old, new)) if a != b]
    runs = []
    for y in ys:
        if runs and y - runs[-1][-1] <= gap:
            runs[-1].append(y)
        else:
            runs.append([y])
    boxes, n = [], len(new[0])
    for run in runs:
        x0, x1 = n, 0
        for y in run:  # xor of the rows as big integers: lowest and highest differing byte, no per-byte loop
            d = int.from_bytes(old[y], "big") ^ int.from_bytes(new[y], "big")
            x0, x1 = min(x0, n - 1 - (d.bit_length() - 1) // 8), max(x1, n - ((d & -d).bit_length() - 1) // 8)
        boxes.append((x0, run[0], x1, run[-1] + 1))
    return boxes


def sixel(rows, keys):
    """Rows of palette registers, all the same length -> DEC Sixel image; keys: the palette's 15-bit colours."""
    n = len(rows[0])
    out = [b'\x1bPq"1;1;%d;%d' % (n, len(rows))]
    pct = lambda v: round(v * 100 / 31)
    out += [b"#%d;2;%d;%d;%d" % (i, pct(keys[i] >> 10), pct(keys[i] >> 5 & 31), pct(keys[i] & 31))
            for i in sorted(set(b"".join(rows)))]  # only the registers this image uses
    offset = int.from_bytes(b"?" * n, "big")  # sixel characters start at '?' (63)
    for y in range(0, len(rows), 6):
        band, layers = rows[y:y + 6], []
        for c in set(b"".join(band)):  # colours present in this band
            bits = sum(int.from_bytes(r.translate(_IS[c]), "big") << k for k, r in enumerate(band)) + offset
            layers.append(b"#%d" % c + _RLE.sub(lambda m: b"!%d%c" % (len(m[0]), m[1][0]), bits.to_bytes(n, "big")))
        out.append(b"$".join(layers) + b"-")
    return b"".join(out) + b"\x1b\\"


def cell_refiner():
    """Windows Terminal reports the mouse by cell only (no SGR-pixel mode), so the guest pointer would snap to cell
    centres. On Windows, pair each report with the host cursor (GetCursorPos, physical pixels) and fit, per axis,
    cell = a + b * pixel over recent reports: the fit places the pointer within the cell. Returns refine(col, row)
    -> fractional (col, row), centre of a cell = its integer; elsewhere, or until the fit has data, the cell itself."""
    if not WINDOWS:
        return lambda c, r: (c, r)
    import ctypes
    user32, pt, samples = ctypes.windll.user32, (ctypes.c_long * 2)(), []
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical pixels, as the terminal draws
    except AttributeError:
        pass

    def refine(c, r):
        user32.GetCursorPos(pt)
        samples.append(((c, r), (pt[0], pt[1])))
        del samples[:-200]
        out = []
        for i, cell in enumerate((c, r)):
            cs, ps = [s_[0][i] for s_ in samples], [s_[1][i] for s_ in samples]
            mc, mp = sum(cs) / len(cs), sum(ps) / len(ps)
            var = sum((v - mp) ** 2 for v in ps)
            if len(set(cs)) < 3 or not var:
                out.append(cell); continue
            f = mc + sum((v - mp) * (u - mc) for v, u in zip(ps, cs)) / var * (pt[i] - mp)
            if abs(f - cell) > 1:  # window moved or resized: start the fit again
                samples[:] = samples[-1:]
                f = cell
            out.append(min(cell + 0.5, max(cell - 0.5, f)))  # never leave the reported cell
        return tuple(out)
    return refine


def raw_terminal():
    """Raw keyboard and mouse input, VT output; returns the function that restores the terminal."""
    if WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        handles, modes = (k.GetStdHandle(-10), k.GetStdHandle(-11)), []
        for h in handles:
            m = ctypes.c_uint32(); k.GetConsoleMode(h, ctypes.byref(m)); modes.append(m.value)
        cp = k.GetConsoleCP()
        k.SetConsoleCP(65001)
        k.SetConsoleMode(handles[0], 0x280)  # VT input + extended flags: raw keys and mouse, no quick-edit selection
        k.SetConsoleMode(handles[1], modes[1] | 0x4)  # VT output
        return lambda: (k.SetConsoleCP(cp), [k.SetConsoleMode(h, m) for h, m in zip(handles, modes)])
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    return lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)


def view(name, fps=30, read_only=False):
    proto = graphics()
    if not proto:
        print("This terminal shows no inline images (Kitty or Sixel); opening the native window instead.", file=sys.stderr)
        return show(name, read_only)
    m = load(name)
    layout, family = m.get("layout", "us"), PROFILES[m.get("os", "linux")]["family"]
    # own QMP monitor and capture file, so agents keep working while someone watches
    q, out = QMP(endpoint(name, "qmp-view")), sys.stdout.buffer
    restore = raw_terminal()
    keys = queue.Queue()  # a reader thread: select() does not take console handles on Windows

    def reader():
        while True:
            chunk = os.read(sys.stdin.fileno(), 65536)
            keys.put(chunk)
            if not chunk:
                return
    threading.Thread(target=reader, daemon=True).start()
    # alt screen, hide cursor, all mouse motion as SGR (in pixels where the terminal can), ask the cell size in pixels
    out.write(b"\x1b[?1049h\x1b[?25l\x1b[?1003h\x1b[?1006h" + (b"\x1b[?1016h" if proto == "kitty" else b"") + b"\x1b[16t")
    last, geom, held, cell, shown, prev_rgb, refine = None, None, set(), (20, 10), None, b"", cell_refiner()
    stale, mapped0 = False, 0
    active = time.monotonic()  # last screen change or input: capture at fps for 2 s after it, at 5 Hz when idle
    try:
        while True:
            try:
                data, (w, h) = screenshot(name, q, "view.png") if proto == "kitty" else screen_pixels(name, q)
            except (OSError, RuntimeError):  # a capture can stall >10 s on a loaded host: reconnect, keep viewing
                q.close()
                if state(name) == "stopped":
                    return
                time.sleep(0.5)
                q = QMP(endpoint(name, "qmp-view"))
                continue
            g = term_geometry(cell)
            if data != last or g != geom:
                active = time.monotonic()
                cols, rows, cw, ch = g
                if g != geom:  # a fresh alternate screen: ED 2 leaves Sixel images behind in Windows Terminal
                    out.write(b"\x1b[?1049l\x1b[?1049h\x1b[2J")
                    shown = None
                last, geom = data, g
                if proto == "kitty":
                    cu, ru = fit(w, h, cols, rows, cw, ch)
                    dw, dh = cu * cw, ru * ch  # displayed size in pixels
                    ox, oy = (cols - cu) // 2, (rows - 1 - ru) // 2  # centred, in cells
                    b64 = base64.standard_b64encode(data)
                    out.write(b"\x1b[%d;%dH" % (oy + 1, ox + 1))
                    for i in range(0, len(b64), 4096):
                        head = b"a=T,f=100,i=1,c=%d,r=%d,C=1,q=2," % (cu, ru) if i == 0 else b""
                        out.write(b"\x1b_G%sm=%d;%s\x1b\\" % (head, i + 4096 < len(b64), b64[i:i + 4096]))
                else:  # Sixel has no scaling: scale here to fill the window above the status line
                    # shortcut: at most 2x up, the cost grows with the output; raise it for very large windows
                    scale = min(cols * cw / w, (rows - 1) * ch / h, 2)
                    dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
                    ox, oy = int((cols * cw - dw) / 2 // cw), int(((rows - 1) * ch - dh) / 2 // ch)
                    same = shown is not None and len(prev_rgb) == len(data)
                    if not same:  # the palette is built at start and on resize only: rebuilding it on every new
                        palette = make_palette(data, w, h)  # screen cost ~0.3 s, new colours take their nearest
                    frame = sixel_rows(data, w, h, dw, dh, palette, prev_rgb, shown) if same else sixel_rows(data, w, h, dw, dh, palette)
                    prev_rgb = data
                    # many colours approximated since the palette was made (the screen changed a lot): a new one
                    # once nothing has moved for 2 s. Counted from the first frame, whose smoothed shades are expected.
                    mapped = len(palette[1]) - palette[1].count(255)
                    if not same:
                        mapped0 = mapped
                    stale = mapped - mapped0 > 400
                    boxes = [(0, 0, dw, dh)] if shown is None or cw % 1 or ch % 1 else changed(shown, frame, int(ch))
                    for box in boxes:  # redraw only the cells that changed: a moving pointer costs a few cells
                        cw_, ch_ = int(cw), int(ch)
                        x0, y0 = box[0] // cw_ * cw_, box[1] // ch_ * ch_
                        y1 = min(dh, y0 + -(-(box[3] - y0) // 6) * 6)  # whole sixel bands: no blank rows below
                        out.write(b"\x1b[%d;%dH" % (oy + y0 // ch_ + 1, ox + x0 // cw_ + 1))
                        out.write(sixel([r[x0:box[2]] for r in frame[y0:y1]], palette[0]))
                    shown = frame
                out.write(f"\x1b[{rows};1H\x1b[2K\x1b[2m {name} · {w}x{h} · Ctrl+] exit · Ctrl+O window\x1b[0m".encode())
                out.flush()
            try:
                chunk = keys.get(timeout=1 / fps if time.monotonic() - active < 2 else 0.2)
                active = time.monotonic()
                while not keys.empty() and chunk:  # take everything typed meanwhile
                    chunk += keys.get_nowait()
            except queue.Empty:
                if stale and time.monotonic() - active > 2:  # never while someone is moving or typing
                    last = shown = None  # full redraw with a palette made for this screen
                    stale = False
                continue
            if not chunk:
                return
            events = parse_input(chunk.decode("utf-8", "replace"))
            motion = lambda e: e[0] == "mouse" and e[1] & 32 and e[1] & 3 == 3  # pointer moves, no button held
            for i, e in enumerate(events):
                if e[0] == "quit":
                    return
                if e[0] == "cell":
                    cell = e[1:]
                elif e[0] == "show":
                    try:
                        show(name, read_only)
                    except SystemExit as err:  # no VNC viewer installed: say so on the status line, keep viewing
                        out.write(f"\x1b[{rows};1H\x1b[2K\x1b[33m {err}\x1b[0m".encode()); out.flush()
                elif read_only:
                    continue
                elif e[0] == "text":
                    type_text(q, "".join(c for c in e[1] if c.isprintable()), layout, family)
                elif e[0] == "key":
                    send_keys(q, chord_keys(e[1], layout))
                elif e[0] == "mouse":
                    if motion(e) and i + 1 < len(events) and motion(events[i + 1]):
                        continue  # only the latest of a run of moves: the pointer follows without a backlog
                    code, px, py, pressed = e[1:]
                    if proto == "sixel":  # 1-based cells, refined within the cell where the host allows it
                        px, py = refine(px, py)
                        px, py = (px - 0.5) * cw, (py - 0.5) * ch
                    x, y = (px - ox * cw) * w / dw, (py - oy * ch) * h / dh
                    if not (0 <= x < w and 0 <= y < h):
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
        out.write((b"\x1b_Ga=d,i=1\x1b\\" if proto == "kitty" else b"") + b"\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?25h\x1b[?1049l")
        out.flush()
        restore()


# ---------- guided setup, diagnostics, skills ----------

# Fedora's virtio-win build, pinned: the "stable" link redirects over plain HTTP and only RPMs have checksums.
GUEST_TOOLS = ("https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/archive-virtio/virtio-win-0.1.302-1/"
               "virtio-win-guest-tools.exe", "d8ae9ea1e943384ac195e012cd36f82c3161620e5fc9529be4e42e6f420e222d")
GUEST_TOOLS_OS = ("win10", "win11", "win2016", "win2019", "win2022", "win2025")  # what virtio-win 0.1.302 supports


def default_image(plan=False, os_name="linux"):
    """Latest Debian live XFCE ISO (cached, SHA-256 verified), or Microsoft's evaluation ISO."""
    if os_name != "linux":
        return windows_image(os_name, plan)
    index =urllib.request.urlopen(IMAGE_INDEX, timeout=30).read().decode()
    iso = re.search(IMAGE_PATTERN, index)[0]
    dest = os.path.join(STORE, "images", iso)
    if os.path.exists(dest) or plan:
        return dest
    sums = urllib.request.urlopen(IMAGE_INDEX + "SHA256SUMS", timeout=30).read().decode()
    expected = re.search(r"([0-9a-f]{64})\s+" + re.escape(iso), sums)[1]
    return download(IMAGE_INDEX + iso, dest, expected)


def download(url, dest, sha256):
    """HTTPS download to dest, kept only if its SHA-256 matches."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    digest, part, label = hashlib.sha256(), dest + ".part", os.path.basename(dest)
    with urllib.request.urlopen(url, timeout=60) as r:
        if urllib.parse.urlsplit(r.geturl()).scheme != "https":  # checked before any file is written
            raise SystemExit(f"{url} redirected off HTTPS ({r.geturl()}); aborting")
        with open(part, "wb") as f:
            total, done = int(r.headers.get("Content-Length", 0)), 0
            while chunk := r.read(1 << 20):
                f.write(chunk); digest.update(chunk); done += len(chunk)
                if total:
                    print(f"\rdownloading {label}: {done * 100 // total}%", end="", file=sys.stderr)
    print(file=sys.stderr)
    if digest.hexdigest() != sha256:
        os.unlink(part)
        raise SystemExit(f"checksum mismatch for {label}; download discarded")
    os.rename(part, dest)
    return dest


def guest_tools():
    """virtio-win guest tools (VirtIO serial driver + qemu-guest-agent) for Windows 10/Server 2016 and later,
    cached; None when they cannot be fetched (the guest then works without its agent)."""
    url, sha256 = GUEST_TOOLS
    dest = os.path.join(STORE, "images", "virtio-win-guest-tools-0.1.302.exe")
    if os.path.exists(dest):
        return dest
    try:
        return download(url, dest, sha256)
    except (OSError, SystemExit) as e:
        print(f"guest tools not installed ({e}): status users, push and pull will be unavailable", file=sys.stderr)
        return None


def windows_image(os_name, plan=False):
    """Official evaluation media straight from Microsoft over HTTPS (no published checksum to verify).
    Other Windows versions need the user's own ISO."""
    url = WINDOWS_IMAGES.get(os_name)
    if not url:
        raise SystemExit(f"{os_name}: provide your installation ISO with --iso (only "
                         f"{', '.join(WINDOWS_IMAGES)} can be downloaded automatically)")
    dest = os.path.join(STORE, "images", f"{os_name}-eval.iso")
    if os.path.exists(dest) or plan:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as r, open(dest + ".part", "wb") as f:
        final = urllib.parse.urlsplit(r.geturl())  # no checksum is published: the channel is the only guarantee
        if final.scheme != "https" or not (final.hostname or "").endswith(".microsoft.com"):
            raise SystemExit(f"unexpected download location {r.geturl()}; aborting")
        total, done = int(r.headers.get("Content-Length", 0)), 0
        while chunk := r.read(1 << 20):
            f.write(chunk); done += len(chunk)
            if total:
                print(f"\rdownloading {os_name} evaluation: {done * 100 // total}%", end="", file=sys.stderr)
    print(file=sys.stderr)
    if total and done != total:
        raise SystemExit("download incomplete; run again")
    os.rename(dest + ".part", dest)
    return dest


def up(name, iso=None, no_view=False, plan=False, **opts):
    exists = os.path.exists(os.path.join(vm_dir(name), "machine.json"))
    st = state(name) if exists else "stopped"
    os_name = opts.get("os_name", "linux")
    steps = []
    if not exists:
        what = "Debian live XFCE (~4 GB, SHA-256 verified)" if os_name == "linux" else f"{os_name} evaluation from Microsoft"
        steps += ([] if iso else [f"download {what}"]) + [f"create {name} ({os_name})"]
        if os_name != "linux":
            steps.append("unattended Windows install on first start (10-40 min)")
    if st == "stopped":
        steps.append(f"start {name}")
    elif st == "paused":
        steps.append(f"resume {name}")
    if not no_view:
        steps.append(f"view {name}")
    if plan:
        image = None if exists else iso or default_image(plan=True, os_name=os_name)
        print(json.dumps({"plan": steps, "image": image}, indent=1)); return
    if not exists:
        create(name, iso or default_image(os_name=os_name), **opts)
    if st == "stopped":
        start(name)
    elif st == "paused":
        pause(name, resume=True)
    if not no_view and sys.stdin.isatty():
        view(name)
    else:
        print(json.dumps({"ready": name, "next": f"terminal-vm view {name}  (or: terminal-vm mcp {name})"}))


def doctor():
    checks = []
    def check(name, ok, detail, required=True):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "required": required})
    def found(tool):
        try:
            return qemu_bin(tool)
        except SystemExit:
            return None
    qemu, img = found("qemu-system-x86_64"), found("qemu-img")
    ver = subprocess.run([qemu, "--version"], capture_output=True, text=True).stdout.split("\n")[0] if qemu else ""
    num = tuple(int(x) for x in re.search(r"(\d+)\.(\d+)", ver).groups()) if ver else (0, 0)
    install = "winget install SoftwareFreedomConservancy.QEMU" if WINDOWS else "apt install qemu-system-x86 qemu-utils"
    check("qemu", num >= ((7, 2) if WINDOWS else (7, 1)), ver or f"qemu-system-x86_64 not found ({install})")  # 7.2: unix sockets on Windows
    check("qemu-img", img, img or install)
    if WINDOWS:
        probe = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "(Get-CimInstance Win32_OptionalFeature -Filter \"Name='HypervisorPlatform'\").InstallState"],
                               capture_output=True, text=True).stdout.strip()
        check("whpx", probe == "1", "Windows Hypervisor Platform enabled" if probe == "1" else
              "disabled: VMs run very slowly. As administrator: Enable-WindowsOptionalFeature -Online "
              "-FeatureName HypervisorPlatform, then restart", required=False)
    else:
        check("kvm", os.access("/dev/kvm", os.R_OK | os.W_OK),
              "ok" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "no access to /dev/kvm: sudo usermod -aG kvm $USER, then log in again")
        if in_wsl():
            check("wsl", False, "WSL can stop this distribution when no WSL terminal is open, killing its VMs "
                  "mid-install: keep one open (wsl -d <distro> -- sleep infinity)", required=False)
    check("python", sys.version_info >= (3, 8), sys.version.split()[0])
    viewer = next((e for e, _ in VIEWERS if shutil.which(e)), None)
    check("window viewer", viewer, viewer or ("optional: winget install TigerVNC.TigerVNC" if WINDOWS
                                              else "optional: apt install tigervnc-viewer"), required=False)
    if MAC:
        check("terminal-mac", mac_bin(), mac_bin() or "for macOS guests: install terminal-mac", required=False)
    check("terminal graphics", graphics(), f"{graphics()} images in this terminal" if graphics()
          else "optional: run view in Windows Terminal, Kitty, Ghostty, WezTerm or foot", required=False)
    print(json.dumps(checks, indent=1))
    if not all(c["ok"] for c in checks if c["required"]):
        sys.exit(1)


def skills(cmd, dest=None):
    if cmd == "get":
        with open(SKILL) as f:
            print(f.read(), end=""); return
    if cmd == "path":
        print(SKILL); return
    dest = os.path.join(dest or os.path.expanduser("~/.claude/skills"), "terminal-vm")
    os.makedirs(dest, exist_ok=True)
    shutil.copy(SKILL, os.path.join(dest, "SKILL.md"))
    print(json.dumps({"installed": os.path.join(dest, "SKILL.md")}))


# ---------- agent authority ----------
# Through MCP, agents run the lifecycle of sandboxed VMs and of the VMs they created themselves (marked
# created_by=agent at creation; only the CLI can change that). The user's own VMs stay off-limits. Risky steps need
# the user's approval: approve(message, cli) asks (MCP elicitation) or raises, naming the CLI command to ask for.
AGENT_LOG = "agent.log"


def agent_owned(name):
    return load(name).get("created_by") == "agent"


def managed(name):
    m = load(name)
    if not (m.get("sandbox") or m.get("created_by") == "agent"):
        raise SystemExit(f"{name} belongs to the user: agents manage only sandboxed VMs and the VMs they created")
    return m


def quiet(f, *a, **k):
    """Call a CLI function that prints its JSON result (stdout is the MCP channel) and return that result."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = f(*a, **k)
    if r is not None:
        return r
    out = buf.getvalue().strip()
    try:
        return json.loads(out) if out else {}
    except ValueError:
        return json.loads(out.splitlines()[-1])


def _net_owned(net):
    return net_load(net).get("created_by") == "agent"


def agent_call(tool, a, approve):
    vm, net = a.get("vm"), a.get("net")
    if tool == "vm_list":
        return [dict(v, managed=v.get("engine") is None and bool(load(v["name"]).get("sandbox") or agent_owned(v["name"])))
                for v in quiet(ls)]
    if tool == "vm_create":
        os_name, sandbox = a.get("os", "linux"), a.get("sandbox", True)
        if a.get("iso"):  # only images already in the store: never an arbitrary host file as the guest's CD
            iso = os.path.join(STORE, "images", os.path.basename(a["iso"]))
            if not os.path.isfile(iso):
                raise SystemExit(f"no image {os.path.basename(a['iso'])!r} in {os.path.join(STORE, 'images')}")
        else:
            iso = default_image(True, os_name)
            if not os.path.exists(iso):
                approve(f"Download the {os_name} installation image ({os.path.basename(iso)}, several GB)?",
                        f"terminal-vm image --os {os_name}")
                iso = default_image(False, os_name)
        mem, mm = a.get("memory_gb") or PROFILES.get(os_name, {}).get("memory", 1), a.get("max_memory_gb")
        if mm and mm > max(2 * mem, mem + 2):  # above create's default ceiling
            approve(f"Create {vm} with a {mm} GiB memory ceiling?", f"terminal-vm resources {vm} --max-memory {mm}")
        # A non-sandboxed agent VM starts offline too: internet is the user's call (vm_internet)
        out = quiet(create, vm, iso, os_name, a.get("cpus"), a.get("memory_gb"), a.get("disk_gb"),
                    offline=not sandbox, sandbox=sandbox, max_cpus=a.get("max_cpus"), max_memory=mm)
        m = load(vm); m["created_by"] = "agent"; save(vm, m)
        return out
    if tool == "vm_clone":
        src = load(vm)
        users = not (src.get("sandbox") or src.get("created_by") == "agent")
        if users:  # a copy of their disk and account
            approve(f"Clone your VM {vm} as {a['new']} (an offline copy of its disk and account the agent manages)?",
                    f"terminal-vm clone {vm} {a['new']}")
        out = quiet(clone, vm, a["new"])
        m = load(a["new"]); m["created_by"] = "agent"
        if users and not m.get("sandbox"):
            m["offline"] = True  # internet stays the user's call (vm_internet)
        save(a["new"], m)
        return out
    if tool == "net_list":
        return net_ls()
    if tool == "net_create":
        cfg = net_create(net)
        with open(net_file(net), "w") as f:
            json.dump(dict(cfg, created_by="agent"), f)
        return cfg
    if tool == "net_remove":
        if not _net_owned(net):
            raise SystemExit(f"network {net!r} belongs to the user")
        return net_rm(net)
    managed(vm)
    destructive = tool == "vm_remove" or (tool == "vm_snapshot" and a.get("action") in ("load", "rm"))
    if destructive and not agent_owned(vm):
        approve(f"{tool} {a.get('action', '')} on {vm}, your own sandboxed VM?",
                f"terminal-vm rm {vm}" if tool == "vm_remove" else f"terminal-vm snapshot {vm} {a['action']} {a.get('tag')}")
    if tool == "vm_start":
        return quiet(start, vm)
    if tool == "vm_stop":
        return quiet(stop, vm, bool(a.get("force")), int(a.get("timeout", 60)))
    if tool == "vm_remove":
        return quiet(rm, vm)
    if tool == "vm_snapshot":
        return quiet(snapshot, vm, a.get("action", "ls"), a.get("tag"))
    if tool == "vm_resources":
        mc, mm = a.get("max_cpus"), a.get("max_memory_gb")
        if mc or mm:
            approve(f"Raise {vm}'s ceiling to {mc or '-'} CPUs / {mm or '-'} GiB?",
                    f"terminal-vm resources {vm}" + (f" --max-cpus {mc}" if mc else "") + (f" --max-memory {mm}" if mm else ""))
        return resources(vm, a.get("cpus"), a.get("memory_gb"), mc, mm)
    if tool == "vm_internet":
        if a.get("on"):
            approve(f"Give {vm} internet access?", f"terminal-vm internet {vm} on")
        return set_internet(vm, None if a.get("on") is None else "on" if a["on"] else "off")
    if tool in ("net_attach", "net_detach"):
        if tool == "net_attach" and not _net_owned(net):
            approve(f"Plug {vm} into your network {net!r} (it would reach your VMs on it)?", f"terminal-vm net attach {vm} {net}")
        return net_attach(vm, net, tool == "net_attach")
    raise SystemExit(f"unknown tool {tool}")


# ---------- MCP ----------

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}

_N = {"type": "number"}
_LAYOUT = {"layout": {"type": "string", "enum": sorted(LAYOUTS),
                      "description": "Override the VM's keyboard layout for this call (us = QWERTY, fr = AZERTY)"}}
_SHOT = {"screenshot":{"type": "boolean", "description": "Return a fresh screenshot with the result"}}
TOOLS = [
    ("screenshot", "Capture the guest screen. Coordinates in other tools are pixels of this image.", _schema({})),
    ("click", "Click at a point.", _schema({"x": _N, "y": _N, "clicks": {"type": "integer", "enum": [1, 2, 3]},
                                             "button": {"type": "string", "enum": ["left", "right", "middle"]}, **_SHOT}, ["x", "y"])),
    ("move", "Move the pointer.", _schema({"x": _N, "y": _N, **_SHOT}, ["x", "y"])),
    ("drag", "Drag with the left button.", _schema({"x": _N, "y": _N, "x2": _N, "y2": _N, **_SHOT}, ["x", "y", "x2", "y2"])),
    ("scroll", "Scroll; negative dy scrolls down (100 per notch).", _schema({"dy": _N, "x": _N, "y": _N, **_SHOT}, ["dy"])),
    ("type", "Type literal text in the guest's keyboard layout (Unicode beyond it uses Ctrl+Shift+U).",
     _schema({"text": {"type": "string"}, **_LAYOUT, **_SHOT}, ["text"])),
    ("key", "Press a key chord, e.g. ctrl+a, alt+tab, return, super.", _schema({"key": {"type": "string"}, **_LAYOUT, **_SHOT}, ["key"])),
    ("status", "VM run state, screen size and logged-in guest users.", _schema({})),
    ("top", "Every running VM's real host use (CPU % of one core, resident memory GiB) next to its allotment and "
            "ceiling, plus the quota. Read-only.", _schema({})),
    ("quota", "Host-wide quota and its use: running vCPUs, memory ceilings of running VMs (GiB), virtual disk "
              "(GiB), running VMs. Only the user can change it.", _schema({})),
    ("resources", "Show the VM's CPUs and memory and their ceiling, or change them within it: give cpus and/or "
                  "memory_gb. Memory changes live where the guest has a balloon driver; CPUs are added live on Linux "
                  "guests; otherwise the change applies at the next start (the reply says which).",
     _schema({"cpus": {"type": "integer", "minimum": 1}, "memory_gb": {"type": "integer", "minimum": 1}})),
    ("wait", "Wait until the screen stops changing (loading, installing, animations), then return a screenshot. "
             "stable: seconds without change (default 1); timeout: default 30. A blinking cursor can keep it from settling.",
     _schema({"stable": _N, "timeout": _N})),
]
INPUT_TOOLS = {"click", "move", "drag", "scroll", "type", "key"}
_VM, _NET = {"vm": {"type": "string"}}, {"net": {"type": "string"}}
_MANAGED = " Agents manage sandboxed VMs and the VMs they created; the user's other VMs are off-limits."
AGENT_TOOLS = [
    ("vm_list", "Every VM with its state; managed=true marks the ones this agent may run." + _MANAGED, _schema({})),
    ("vm_create", "Create a VM, sandboxed by default (no network, disk reset at each stop). It installs at first "
                  "start. iso: an image already in the store; without it, the default image for os (a download "
                  "needs the user's approval). Non-sandboxed VMs start offline (vm_internet). The quota counts a "
                  "running VM's memory ceiling (default: twice memory_gb, at least +2): set max_memory_gb = "
                  "memory_gb to fit more small VMs.",
     _schema({**_VM, "os": {"type": "string", "enum": sorted(PROFILES)}, "cpus": {"type": "integer", "minimum": 1},
              "max_cpus": {"type": "integer", "minimum": 1}, "max_memory_gb": {"type": "integer", "minimum": 1},
              "memory_gb": {"type": "integer", "minimum": 1}, "disk_gb": {"type": "integer", "minimum": 4},
              "sandbox": {"type": "boolean"}, "iso": {"type": "string"}}, ["vm"])),
    ("vm_clone", "Copy-on-write clone of a stopped VM, in seconds (install once, clone many). Both stay usable. Cloning one of the user's VMs needs their approval; the clone starts offline.",
     _schema({**_VM, "new": {"type": "string"}}, ["vm", "new"])),
    ("vm_start", "Start a managed VM, within the host quota." + _MANAGED, _schema(_VM, ["vm"])),
    ("vm_stop", "Shut a managed VM down (force: power off at once).",
     _schema({**_VM, "force": {"type": "boolean"}, "timeout": {"type": "integer"}}, ["vm"])),
    ("vm_remove", "Delete a stopped managed VM and its disk.", _schema(_VM, ["vm"])),
    ("vm_snapshot", "Snapshots of a managed VM: save, load, rm or ls (tag needed except for ls).",
     _schema({**_VM, "action": {"type": "string", "enum": ["save", "load", "rm", "ls"]}, "tag": {"type": "string"}},
             ["vm", "action"])),
    ("vm_resources", "Show or change a managed VM's CPUs and memory within its ceiling; raising the ceiling "
                     "(max_cpus, max_memory_gb, stopped VM) needs the user's approval.",
     _schema({**_VM, "cpus": {"type": "integer", "minimum": 1}, "memory_gb": {"type": "integer", "minimum": 1},
              "max_cpus": {"type": "integer"}, "max_memory_gb": {"type": "integer"}}, ["vm"])),
    ("vm_internet", "Show (without on) or change a stopped managed VM's internet access; turning it on needs the "
                    "user's approval.", _schema({**_VM, "on": {"type": "boolean"}}, ["vm"])),
    ("net_list", "Isolated networks and their VMs (MAC, suggested static address).", _schema({})),
    ("net_create", "Create an isolated VM-to-VM network (no LAN, no internet, no DHCP).", _schema(_NET, ["net"])),
    ("net_remove", "Remove an empty network this agent created.", _schema(_NET, ["net"])),
    ("net_attach", "Plug a stopped managed VM into a network; set the suggested address in the guest. A network "
                   "the user created needs their approval.", _schema({**_VM, **_NET}, ["vm", "net"])),
    ("net_detach", "Unplug a stopped managed VM from a network.", _schema({**_VM, **_NET}, ["vm", "net"])),
]
AGENT_NAMES = {n for n, _, _ in AGENT_TOOLS}
_OTHER = {"vm": {"type": "string", "description": "Another VM this agent manages (default: this server's VM)"}}
SCREEN_TOOLS = [(n, d, s if n in ("top", "quota") else dict(s, properties={**s["properties"], **_OTHER}))
                for n, d, s in TOOLS]


def image(data):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def mcp(name):
    agents = {}  # per VM: whether qemu-guest-agent answered once; unknown until asked
    seen = {}  # per VM: when a logged-in session was last confirmed
    can_ask, pending, asks = False, collections.deque(), iter(range(1, 1 << 30))

    def record(tool, a, vm, error=None):  # sandbox screen actions, and every infrastructure action, for review
        try:
            journal = os.path.join(vm_dir(vm), "actions.log") if load(vm).get("sandbox") else None
        except SystemExit:  # no such VM
            journal = None
        path = os.path.join(STORE, AGENT_LOG) if tool in AGENT_NAMES else journal
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "arguments": a,
                                    **({"error": error} if error else {})}) + "\n")

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

    def approve(message, cli):
        """Ask the user through the client (MCP elicitation); raise unless they accept."""
        if not can_ask:
            raise RuntimeError("This needs the user's approval and this client cannot ask them. "
                               f"Ask the user to run: {cli}")
        aid = f"ask-{next(asks)}"
        send({"jsonrpc": "2.0", "id": aid, "method": "elicitation/create", "params": {
            "message": message, "requestedSchema": {"type": "object", "properties": {
                "approve": {"type": "boolean", "title": "Approve", "description": message}}, "required": ["approve"]}}})
        while True:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError("the client closed the connection before the user answered")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != aid or "method" in msg:
                pending.append(line)  # other messages wait until the user has answered
                continue
            r = msg.get("result") or {}
            if r.get("action") == "accept" and (r.get("content") or {}).get("approve"):
                return
            raise RuntimeError(f"The user did not approve. They can still run: {cli}")

    while True:
        line = pending.popleft() if pending else sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, method = req.get("id"), req.get("method")
        if rid is None or method is None:
            continue  # notification, or a stray response
        if method == "initialize":
            can_ask = "elicitation" in (req.get("params", {}).get("capabilities") or {})
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "terminal-vm", "version": VERSION}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, d, s in SCREEN_TOOLS + AGENT_TOOLS]}})
        elif method == "tools/call":
            p = req.get("params", {})
            tool, a = p.get("name"), dict(p.get("arguments") or {})
            vm = name if tool in AGENT_NAMES else a.pop("vm", None) or name
            try:
                if vm != name:  # screen tools on another VM: only one this agent manages
                    managed(vm)
                # one guest-agent round trip per burst of input, not per click: a session seen < 5 s ago still holds
                if tool in INPUT_TOOLS and agents.get(vm) is not False and time.time() - seen.get(vm, 0) > 5:
                    users = guest_users(vm)
                    agents[vm] = users is not None
                    seen[vm] = time.time() if users else 0
                    if users == []:
                        raise RuntimeError("The guest has no logged-in session (login screen). Ask the user to sign in: "
                                           f"terminal-vm view {vm}")
                # resources opens its own QMP session (QEMU takes one client at a time), so not through act()
                out = (agent_call(tool, a, approve) if tool in AGENT_NAMES else
                       resources(vm, a.get("cpus"), a.get("memory_gb")) if tool == "resources" else
                       quota() if tool == "quota" else top() if tool == "top" else act(vm, tool, a))
                record(tool, a, vm)
                if tool == "screenshot":
                    content = [image(out[0])]
                else:
                    content = [{"type": "text", "text": json.dumps(out)}]
                    if a.get("screenshot") or tool == "wait":
                        time.sleep(0.3); content.append(image(screenshot(vm)[0]))
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": content}})
            except (Exception, SystemExit) as e:  # report tool failures to the agent instead of dying
                record(tool, a, vm, str(e))
                send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})


# ---------- CLI ----------

def main(argv=None):
    if (sys.argv[1:] if argv is None else argv)[:1] == ["_switch"]:  # internal: started by start for its networks
        return net_switch((sys.argv[2:] if argv is None else argv[1:])[0])
    mac_args = macos_handoff(sys.argv[1:] if argv is None else argv)
    if mac_args is not None:
        sys.exit(run_mac(mac_args))
    p = argparse.ArgumentParser(prog="terminal-vm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"terminal-vm {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def sizes(sp):
        sp.add_argument("--os", dest="os_name", default="linux", choices=list(PROFILES))
        sp.add_argument("--product-key"); sp.add_argument("--ui-language", default="en-US", help="Windows: language of the ISO")
        sp.add_argument("--cpus", type=int, help="default: per --os")
        sp.add_argument("--memory", type=int, help="GiB, default: per --os"); sp.add_argument("--disk", type=int, help="GiB, default: per --os")
        sp.add_argument("--width", type=int, default=1280); sp.add_argument("--height", type=int, default=800)
        sp.add_argument("--layout", default="us", choices=sorted(LAYOUTS)); sp.add_argument("--offline", action="store_true")
        sp.add_argument("--sandbox", action="store_true", help="no network or file sharing, disposable disk, actions logged")
        sp.add_argument("--max-cpus", type=int, help="ceiling agents may raise CPUs to (default: per --os, host-capped)")
        sp.add_argument("--max-memory", type=int, help="ceiling in GiB agents may raise memory to (default: per --os)")
    u = sub.add_parser("up"); u.add_argument("name", nargs="?", default="default"); u.add_argument("--iso")
    u.add_argument("--no-view", action="store_true"); u.add_argument("--plan", action="store_true"); sizes(u)
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("--iso"); sizes(c)
    s = sub.add_parser("start"); s.add_argument("name"); s.add_argument("--window", action="store_true")
    t = sub.add_parser("stop"); t.add_argument("name"); t.add_argument("--force", action="store_true")
    t.add_argument("--timeout", type=int, default=60)
    for n in ("status", "hide", "pause", "resume", "rm", "reset"):
        sub.add_parser(n).add_argument("name")
    for n in ("view", "show"):
        v = sub.add_parser(n); v.add_argument("name"); v.add_argument("--read-only", action="store_true")
    sub.add_parser("ls"); sub.add_parser("doctor"); sub.add_parser("help"); sub.add_parser("version")
    cl = sub.add_parser("clone"); cl.add_argument("src"); cl.add_argument("new")
    sn = sub.add_parser("snapshot"); sn.add_argument("name"); sn.add_argument("snap_cmd", choices=["save", "load", "rm", "ls"])
    sn.add_argument("tag", nargs="?")
    di = sub.add_parser("display"); di.add_argument("name")
    di.add_argument("--width", type=int, required=True); di.add_argument("--height", type=int, required=True)
    im = sub.add_parser("image"); im.add_argument("--os", dest="os_name", default="linux", choices=list(PROFILES))
    im.add_argument("--plan", action="store_true")
    for n in ("push", "pull"):
        x = sub.add_parser(n); x.add_argument("name"); x.add_argument("src"); x.add_argument("dest")
    m = sub.add_parser("mcp"); m.add_argument("name", nargs="?", default="default")
    k = sub.add_parser("skills"); k.add_argument("skill_cmd", choices=["get", "path", "install"]); k.add_argument("--dir")
    a = sub.add_parser("action"); a.add_argument("name"); a.add_argument("tool"); a.add_argument("args", nargs="*")
    a.add_argument("--output"); a.add_argument("--button", default="left"); a.add_argument("--double", action="store_true")
    a.add_argument("--dy", type=int); a.add_argument("--x", type=float); a.add_argument("--y", type=float)
    a.add_argument("--timeout", type=float, default=30); a.add_argument("--stable", type=float, default=1)
    a.add_argument("--layout", choices=sorted(LAYOUTS), help="this call only: us (QWERTY) or fr (AZERTY)")
    lo = sub.add_parser("layout"); lo.add_argument("name"); lo.add_argument("layout", nargs="?", choices=sorted(LAYOUTS))
    sb = sub.add_parser("sandbox"); sb.add_argument("name"); sb.add_argument("value", nargs="?", choices=["on", "off"])
    sub.add_parser("top")
    ne = sub.add_parser("net"); nsub = ne.add_subparsers(dest="net_cmd", required=True)
    for c in ("create", "rm"):
        nsub.add_parser(c).add_argument("net")
    nsub.add_parser("ls")
    for c in ("attach", "detach"):
        x = nsub.add_parser(c); x.add_argument("name"); x.add_argument("net")
    it = sub.add_parser("internet"); it.add_argument("name"); it.add_argument("value", nargs="?", choices=["on", "off"])
    qu = sub.add_parser("quota")
    for k, h in (("cpus", "running vCPUs"), ("memory", "GiB of running VMs' memory ceilings"),
                 ("disk", "GiB of virtual disks"), ("vms", "running VMs")):
        qu.add_argument(f"--{k}", type=int, help=h)
    rs = sub.add_parser("resources"); rs.add_argument("name"); rs.add_argument("--cpus", type=int)
    rs.add_argument("--memory", type=int, help="GiB")
    rs.add_argument("--max-cpus", type=int, help="new ceiling (stopped VM)")
    rs.add_argument("--max-memory", type=int, help="new ceiling in GiB (stopped VM)")
    o = p.parse_args(argv)

    opts = ({k: getattr(o, k) for k in ("os_name", "cpus", "memory", "disk", "width", "height", "layout", "offline", "product_key", "ui_language", "sandbox", "max_cpus", "max_memory")}
            if o.cmd in ("up", "create") else {})
    try:
        if o.cmd == "up": return up(o.name, o.iso, o.no_view, o.plan, **opts)
        if o.cmd == "create": return create(o.name, o.iso or default_image(os_name=o.os_name), **opts)
        if o.cmd == "start": return start(o.name, o.window)
        if o.cmd == "stop": return stop(o.name, o.force, o.timeout)
        if o.cmd in ("pause", "resume"): return pause(o.name, o.cmd == "resume")
        if o.cmd == "ls": return ls()
        if o.cmd == "doctor": return doctor()
        if o.cmd == "help": return p.print_help()
        if o.cmd == "version": return print(f"terminal-vm {VERSION}")
        if o.cmd == "rm": return rm(o.name)
        if o.cmd == "reset": return reset(o.name)
        if o.cmd == "clone": return clone(o.src, o.new)
        if o.cmd == "snapshot": return snapshot(o.name, o.snap_cmd, o.tag)
        if o.cmd == "display": return display(o.name, o.width, o.height)
        if o.cmd == "layout": return set_layout(o.name, o.layout)
        if o.cmd == "sandbox": return set_sandbox(o.name, o.value)
        if o.cmd == "resources": return print(json.dumps(resources(o.name, o.cpus, o.memory, o.max_cpus, o.max_memory)))
        if o.cmd == "quota":
            change = {"cpus": o.cpus, "memory_gb": o.memory, "disk_gb": o.disk, "vms": o.vms}
            return print(json.dumps(quota(change if any(v is not None for v in change.values()) else None), indent=1))
        if o.cmd == "top": return print(json.dumps(top(), indent=1))
        if o.cmd == "internet": return print(json.dumps(set_internet(o.name, o.value)))
        if o.cmd == "net":
            return print(json.dumps(
                net_create(o.net) if o.net_cmd == "create" else net_rm(o.net) if o.net_cmd == "rm" else
                net_ls() if o.net_cmd == "ls" else net_attach(o.name, o.net, o.net_cmd == "attach"), indent=1))
        if o.cmd == "image": return print(json.dumps({"image": default_image(o.plan, o.os_name)}))
        if o.cmd in ("push", "pull"): return transfer(o.name, o.cmd, o.src, o.dest)
        if o.cmd == "view": return view(o.name, read_only=o.read_only)
        if o.cmd == "show": return show(o.name, o.read_only)
        if o.cmd == "hide": return hide(o.name)
        if o.cmd == "mcp": return mcp(o.name)
        if o.cmd == "skills": return skills(o.skill_cmd, o.dir)
        if o.cmd == "status":
            print(json.dumps(act(o.name, "status", {}))); return
        nums = [float(v) for v in o.args] if o.tool in ("click", "move", "drag") else []
        args = {"screenshot": {}, "status": {}, "wait": {"timeout": o.timeout, "stable": o.stable},
                "click": lambda: {"x": nums[0], "y": nums[1], "button": o.button, "clicks": 2 if o.double else 1},
                "move": lambda: {"x": nums[0], "y": nums[1]},
                "drag": lambda: dict(zip(("x", "y", "x2", "y2"), nums)),
                "scroll": lambda: {k: v for k, v in (("dy", o.dy), ("x", o.x), ("y", o.y)) if v is not None},
                "type": lambda: {"text": " ".join(o.args), "layout": o.layout},
                "key": lambda: {"key": o.args[0], "layout": o.layout}}[o.tool]
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
