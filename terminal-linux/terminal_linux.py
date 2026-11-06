#!/usr/bin/env python3
"""terminal-linux: a separate desktop VM (QEMU) for computer-use agents, viewed from a terminal.

Port of terminal-mac. Nothing runs inside the guest: the host drives QEMU over QMP
(`screendump` for the screen, `input-send-event` / `send-key` for input). The host desktop is
never touched. Stdlib only. Guests: Linux, and Windows XP to Server 2025 (`--os`), installed
unattended. Hosts: Linux (KVM) and Windows (WHPX).

  up [NAME] [--os OS] [--iso PATH] [--no-view] [--plan]   guided: image, create, start, view
  create NAME [--os linux|winxp|win7|win8|win10|win11|win2016|win2019|win2022|win2025]
              [--iso PATH] [--product-key KEY] [--cpus N] [--memory GiB] [--disk GiB]
              [--width 1280] [--height 800] [--layout us|fr] [--offline]
  ls | status NAME | doctor | help | version
  start NAME [--window] | stop NAME [--force] [--timeout 60] | pause NAME | resume NAME
  rm NAME | reset NAME     delete a stopped VM | blank its disk to reinstall
  snapshot NAME save|load|rm TAG | snapshot NAME ls
  display NAME --width W --height H    screen size at the next start
  layout NAME [us|fr]      keyboard layout for type/key (us = QWERTY, fr = AZERTY); action ... --layout: one call
  image [--os OS] [--plan]             download the installation image only
  push NAME LOCAL GUEST | pull NAME GUEST LOCAL   files, through qemu-guest-agent
  view NAME [--read-only]  interactive terminal viewer (Kitty, Ghostty, WezTerm); Ctrl+] exits, Ctrl+O window
  show NAME [--read-only] | hide NAME   native window (VNC viewer)
  action NAME screenshot|click|move|drag|scroll|type|key|wait ...
  mcp [NAME]         stdio MCP server
  skills get | skills path | skills install [--dir DIR]
"""
import argparse, base64, hashlib, json, os, re, secrets, select, shutil, signal, socket, struct, subprocess, sys, \
    time, urllib.parse, urllib.request
from xml.sax.saxutils import escape

WINDOWS = os.name == "nt"
if not WINDOWS:
    import fcntl, termios, tty  # terminal viewer only

VERSION = "0.4.0"
STORE = os.environ.get("TERMINAL_LINUX_HOME") or (
    os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "terminal-linux") if WINDOWS else
    os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "terminal-linux"))
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, "skills", "terminal-linux", "SKILL.md")
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
        raise SystemExit(f"no VM named {name!r}; create it with: terminal-linux up {name}")


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
        raise RuntimeError("VM is paused; run: terminal-linux resume " + name)
    if tool == "wait":  # until two screenshots `stable` seconds apart are identical, or timeout
        start, last, since = time.time(), None, time.time()
        while True:
            data, _ = screenshot(name, q)
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
                  unattend="sif", key="required", cpu="qemu64"),  # XP dislikes host CPU passthrough
    "win7": dict(_WIN, nic="e1000", usb="usb-ehci", memory=2, disk=32, oobe="win7"),
    "win8": dict(_WIN, nic="e1000", memory=2, disk=32, key="required"),
    # Microsoft's public KMS client setup keys: they select the Pro edition, they do not activate it.
    "win10": dict(_WIN, key="W269N-WFGWX-YVC9B-4J6C9-T83GX"),
    "win11": dict(_WIN, key="W269N-WFGWX-YVC9B-4J6C9-T83GX", bypass=True),
    # Evaluation media need no key; image 2 is Standard with the Desktop Experience.
    **{f"win{v}": dict(_WIN, memory=4, disk=64, index=2) for v in ("2016", "2019", "2022", "2025")},
}
WINDOWS_IMAGES = {  # official evaluation media that can be downloaded without a sign-in
    "win2025": "https://go.microsoft.com/fwlink/?linkid=2293312&clcid=0x409&culture=en-us&country=us",
}
LOCALES = {"us": ("en-US", "0409:00000409"), "fr": ("fr-FR", "040c:0000040c")}


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
    # An agent's VM must stay visible and stoppable: no display-off, sleep or lock on idle (the screen went black,
    # then locked), and honour the ACPI power button at the lock screen (Server ignores it there by default).
    machine = "".join(f'<RunSynchronousCommand wcm:action="add"><Order>{i}</Order><Path>{c}</Path></RunSynchronousCommand>'
                      for i, c in enumerate(('reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System '
                                            '/v ShutdownWithoutLogon /t REG_DWORD /d 1 /f',
                                            'powercfg /change monitor-timeout-ac 0',
                                            'powercfg /change standby-timeout-ac 0',
                                            'powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0'), 1))
    # EnableHexNumpad is read at logon: set it in the first session, then reboot so the session the agent gets
    # (auto-logon again) has it. Writing it to the Default profile in specialize was tried and did not reach the user.
    first = ("<FirstLogonCommands>" + "".join(
        f'<SynchronousCommand wcm:action="add"><Order>{i}</Order><CommandLine>{c}</CommandLine></SynchronousCommand>'
        for i, c in enumerate(('reg add "HKCU\\Control Panel\\Input Method" /v EnableHexNumpad /t REG_SZ /d 1 /f',
                               'shutdown /r /t 5'), 1)) + "</FirstLogonCommands>")
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


def qemu_args(name, m, window, vnc_port=None):
    """Full QEMU command line for a VM (pure: no I/O), so every OS profile is testable.
    vnc_port: Windows hosts, VNC on localhost TCP with a password (set over QMP after start)."""
    d, p = vm_dir(name), PROFILES[m.get("os", "linux")]
    accel, cpu = accelerators()
    cpu = p.get("cpu", cpu)
    ep = lambda k: f"unix:{os.path.join(d, k + '.sock')}"
    w, h = m.get("width", 1280), m.get("height", 800)
    vga = p["vga"] if p["vga"] == "cirrus-vga" else f"{p['vga']},xres={w},yres={h}"  # cirrus has no preferred mode
    args = ["-name", name, *accel, "-cpu", cpu, "-machine", p["machine"], "-m", str(m["memory_mb"]), "-smp", str(m["cpus"]),
            "-drive", f"file={os.path.join(d, 'disk.qcow2')},if={p['disk_bus']}",  # QEMU locks the image: one VM per disk
            "-cdrom", m["iso"], "-boot", "order=cd", "-device", vga,
            *(["-usb"] if p["usb"] == "uhci" else ["-device", p["usb"]]), "-device", "usb-tablet",
            "-nic", "none" if m.get("offline") else f"user,model={p['nic']}",
            "-device", "virtio-serial-pci",
            "-chardev", "socket,path=" + os.path.join(d, "qga.sock") + ",server=on,wait=off,id=qga0",
            "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            "-qmp", ep("qmp") + ",server=on,wait=off",
            "-qmp", ep("qmp-view") + ",server=on,wait=off",  # the viewer's own monitor
            # VNC takes a display number; password=on refuses every client until the password is set
            "-vnc", ep("vnc") if vnc_port is None else f"127.0.0.1:{vnc_port - 5900},password=on",
            "-pidfile", os.path.join(d, "qemu.pid"), "-display", ("sdl" if WINDOWS else "gtk") if window else "none"]
    answers = os.path.join(d, "answers")
    if p["unattend"] == "xml" and os.path.isdir(answers):  # Setup reads autounattend.xml from removable drives
        # removable=on: Setup only searches removable media; a fixed USB disk is ignored
        args += ["-drive", f"if=none,id=answers,driver=vvfat,dir={answers},rw=off", "-device", "usb-storage,drive=answers,removable=on"]
    elif p["unattend"] == "sif" and os.path.isdir(answers):  # XP Setup reads winnt.sif from the floppy
        args += ["-drive", f"if=floppy,index=0,driver=vvfat,dir={answers},floppy=on"]
    return args


def create(name, iso, os_name="linux", cpus=None, memory=None, disk=None, width=1280, height=800, layout="us",
           offline=False, product_key=None, ui_language="en-US"):
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
    if not (1 <= cpus <= os.cpu_count() and 1 <= memory <= 512 and 4 <= disk and 640 <= width <= 7680 and 480 <= height <= 4320):
        raise SystemExit("invalid size: check --cpus (<= host CPUs), --memory GiB, --disk GiB (>= 4), --width/--height")
    key = product_key or (p.get("key") if p.get("key") != "required" else None)
    if p.get("key") == "required" and not key:
        raise SystemExit(f"{os_name} setup needs your product key: --product-key XXXXX-XXXXX-XXXXX-XXXXX-XXXXX")
    if key and not re.fullmatch(r"[A-Za-z0-9]{5}(-[A-Za-z0-9]{5}){4}", key):
        raise SystemExit("product key format: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX")
    os.makedirs(d, mode=0o700, exist_ok=True)
    subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", os.path.join(d, "disk.qcow2"), f"{disk}G"],
                   check=True, stdout=subprocess.DEVNULL)
    m = {"os": os_name, "iso": os.path.abspath(iso), "memory_mb": memory * 1024, "cpus": cpus, "width": width,
         "height": height, "layout": layout, "offline": offline}
    if p["family"] == "windows":
        # 12 characters: within XP's 14-character limit, and the suffix meets Server's complexity policy
        # (an unmet policy would stop the unattended install at a password prompt).
        m.update(user="Administrator" if p["unattend"] == "sif" else "user",
                 password=secrets.token_urlsafe(6) + "-Aa1")
        os.makedirs(os.path.join(d, "answers"), exist_ok=True)
        if p["unattend"] == "sif":
            content, filename = winnt_sif(layout, m["password"], key), "winnt.sif"
        else:
            if not re.fullmatch(r"[a-z]{2,3}-[A-Z]{2}", ui_language or ""):
                raise SystemExit("--ui-language must look like en-US or fr-FR (the language of the ISO)")
            content, filename = autounattend(p, layout, m["user"], m["password"], key, ui_language), "autounattend.xml"
        with open(os.path.join(d, "answers", filename), "w", encoding="utf-8", newline="") as f:
            f.write(content)
    with open(os.path.join(d, "machine.json"), "w") as f:
        json.dump(m, f)
    out = {"created": name, "os": os_name, "path": d}
    if p["family"] == "windows":
        out.update(user=m["user"], password=m["password"], note="Windows installs unattended on first start (10-40 min)")
    print(json.dumps(out))


def start(name, window=False):
    d, m = vm_dir(name), load(name)
    if state(name) != "stopped":
        raise SystemExit(f"{name} is already running")
    if len(os.path.join(d, "qmp-view.sock").encode()) >= 108:  # sun_path limit
        raise SystemExit(f"VM folder path too long for its sockets: {d}; shorten the name or set TERMINAL_LINUX_HOME")
    vnc_port = None
    if WINDOWS:
        vnc_port = free_ports(1)[0]
        with open(os.path.join(d, "ports.json"), "w") as f:
            json.dump({"vnc": vnc_port}, f)
    for k in ("qmp", "qmp-view", "qga", "vnc"):  # stale sockets from a crashed VM
        if os.path.exists(os.path.join(d, k + ".sock")):
            os.unlink(os.path.join(d, k + ".sock"))
    log = open(os.path.join(d, "qemu.log"), "ab")
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if WINDOWS
              else {"start_new_session": True})
    subprocess.Popen([qemu_bin("qemu-system-x86_64")] + qemu_args(name, m, window, vnc_port), stdout=log, stderr=log, **detach)
    for _ in range(100):
        if state(name) != "stopped":
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
                out.append({"name": n, "os": m.get("os", "linux"), "status": state(n), "cpus": m["cpus"], "memory_gb": m["memory_mb"] // 1024,
                            "display": f"{m.get('width', 1280)}x{m.get('height', 800)}", "layout": m.get("layout", "us")})
    print(json.dumps(out, indent=1))


def _stopped(name):
    load(name)
    busy = state(name) != "stopped"
    disk = os.path.join(vm_dir(name), "disk.qcow2")
    if not busy and os.path.exists(disk):  # QMP unreachable is not proof: a running QEMU also holds the image lock
        busy = subprocess.run([qemu_bin("qemu-img"), "info", disk], capture_output=True).returncode != 0
    if busy:
        raise SystemExit(f"{name} is running; stop it first: terminal-linux stop {name}")


def rm(name):
    _stopped(name)
    shutil.rmtree(vm_dir(name))
    print(json.dumps({"removed": name}))


def reset(name):
    """Blank disk of the same size, so the next start installs again (Windows answers and password kept).
    Deletes the disk's snapshots too."""
    _stopped(name)
    disk = os.path.join(vm_dir(name), "disk.qcow2")
    info = subprocess.run([qemu_bin("qemu-img"), "info", "--output=json", disk], check=True, capture_output=True, text=True)
    subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", disk + ".new", str(json.loads(info.stdout)["virtual-size"])],
                   check=True, stdout=subprocess.DEVNULL)
    os.replace(disk + ".new", disk)  # the old disk survives a failed create
    print(json.dumps({"reset": name}))


def snapshot(name, cmd, tag=None):
    """qcow2 internal snapshots. Running VM: whole machine, memory included (savevm, pauses it a few
    seconds). Stopped VM: disk only (qemu-img); loading one then boots from that disk state."""
    load(name)
    if cmd != "ls" and not re.fullmatch(r"[\w.-]{1,64}", tag or ""):
        raise SystemExit(f"snapshot {cmd} needs a TAG (letters, digits, . _ -)")
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


def transfer(name, direction, src, dest):
    """Copy a file to (push) or from (pull) the guest through qemu-guest-agent."""
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
        raise SystemExit(f"{name} is not running; run: terminal-linux start {name}")
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
                     f"or restart with a window: terminal-linux stop {name}; terminal-linux start {name} --window")


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


def view(name, fps=8, read_only=False):
    if WINDOWS or not kitty_supported():  # Windows consoles have no Kitty graphics
        print("This terminal has no Kitty graphics; opening the native window instead.", file=sys.stderr)
        return show(name, read_only)
    m = load(name)
    layout, family = m.get("layout", "us"), PROFILES[m.get("os", "linux")]["family"]
    # own QMP monitor and capture file, so agents keep working while someone watches
    q, out, fd = QMP(endpoint(name, "qmp-view")), sys.stdout, sys.stdin.fileno()
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
                    show(name, read_only)
                elif read_only:
                    continue
                elif e[0] == "text":
                    type_text(q, "".join(c for c in e[1] if c.isprintable()), layout, family)
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
        print(json.dumps({"ready": name, "next": f"terminal-linux view {name}  (or: terminal-linux mcp {name})"}))


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
        try:
            with open("/proc/version") as f:
                wsl = "microsoft" in f.read().lower()
        except OSError:
            wsl = False
        if wsl:
            check("wsl", False, "WSL can stop this distribution when no WSL terminal is open, killing its VMs "
                  "mid-install: keep one open (wsl -d <distro> -- sleep infinity)", required=False)
    check("python", sys.version_info >= (3, 8), sys.version.split()[0])
    viewer = next((e for e, _ in VIEWERS if shutil.which(e)), None)
    check("window viewer", viewer, viewer or ("optional: winget install TigerVNC.TigerVNC" if WINDOWS
                                              else "optional: apt install tigervnc-viewer"), required=False)
    check("terminal graphics", kitty_supported(), "Kitty graphics available" if kitty_supported()
          else "optional: run view in Kitty, Ghostty or WezTerm", required=False)
    print(json.dumps(checks, indent=1))
    if not all(c["ok"] for c in checks if c["required"]):
        sys.exit(1)


def skills(cmd, dest=None):
    if cmd == "get":
        with open(SKILL) as f:
            print(f.read(), end=""); return
    if cmd == "path":
        print(SKILL); return
    dest = os.path.join(dest or os.path.expanduser("~/.claude/skills"), "terminal-linux")
    os.makedirs(dest, exist_ok=True)
    shutil.copy(SKILL, os.path.join(dest, "SKILL.md"))
    print(json.dumps({"installed": os.path.join(dest, "SKILL.md")}))


# ---------- MCP ----------

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}

_N = {"type": "number"}
_LAYOUT = {"layout": {"type": "string", "enum": sorted(LAYOUTS),
                      "description": "Override the VM's keyboard layout for this call (us = QWERTY, fr = AZERTY)"}}
_SHOT = {"screenshot":{"type": "boolean", "description": "Return a fresh screenshot with the result"}}
TOOLS = [
    ("screenshot", "Capture the guest Linux screen. Coordinates in other tools are pixels of this image.", _schema({})),
    ("click", "Click at a point.", _schema({"x": _N, "y": _N, "clicks": {"type": "integer", "enum": [1, 2, 3]},
                                             "button": {"type": "string", "enum": ["left", "right", "middle"]}, **_SHOT}, ["x", "y"])),
    ("move", "Move the pointer.", _schema({"x": _N, "y": _N, **_SHOT}, ["x", "y"])),
    ("drag", "Drag with the left button.", _schema({"x": _N, "y": _N, "x2": _N, "y2": _N, **_SHOT}, ["x", "y", "x2", "y2"])),
    ("scroll", "Scroll; negative dy scrolls down (100 per notch).", _schema({"dy": _N, "x": _N, "y": _N, **_SHOT}, ["dy"])),
    ("type", "Type literal text in the guest's keyboard layout (Unicode beyond it uses Ctrl+Shift+U).",
     _schema({"text": {"type": "string"}, **_LAYOUT, **_SHOT}, ["text"])),
    ("key", "Press a key chord, e.g. ctrl+a, alt+tab, return, super.", _schema({"key": {"type": "string"}, **_LAYOUT, **_SHOT}, ["key"])),
    ("status", "VM run state, screen size and logged-in guest users.", _schema({})),
    ("wait", "Wait until the screen stops changing (loading, installing, animations), then return a screenshot. "
             "stable: seconds without change (default 1); timeout: default 30. A blinking cursor can keep it from settling.",
     _schema({"stable": _N, "timeout": _N})),
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
                    if a.get("screenshot") or tool == "wait":
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
        sp.add_argument("--os", dest="os_name", default="linux", choices=list(PROFILES))
        sp.add_argument("--product-key"); sp.add_argument("--ui-language", default="en-US", help="Windows: language of the ISO")
        sp.add_argument("--cpus", type=int, help="default: per --os")
        sp.add_argument("--memory", type=int, help="GiB, default: per --os"); sp.add_argument("--disk", type=int, help="GiB, default: per --os")
        sp.add_argument("--width", type=int, default=1280); sp.add_argument("--height", type=int, default=800)
        sp.add_argument("--layout", default="us", choices=sorted(LAYOUTS)); sp.add_argument("--offline", action="store_true")
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
    o = p.parse_args(argv)

    opts = ({k: getattr(o, k) for k in ("os_name", "cpus", "memory", "disk", "width", "height", "layout", "offline", "product_key", "ui_language")}
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
        if o.cmd == "version": return print(f"terminal-linux {VERSION}")
        if o.cmd == "rm": return rm(o.name)
        if o.cmd == "reset": return reset(o.name)
        if o.cmd == "snapshot": return snapshot(o.name, o.snap_cmd, o.tag)
        if o.cmd == "display": return display(o.name, o.width, o.height)
        if o.cmd == "layout": return set_layout(o.name, o.layout)
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
