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
              [--width 1280] [--height 800] [--layout us|fr] [--offline] [--project P]
  ls | status NAME | doctor | help | version
  start NAME [--window] | stop NAME [--force] [--timeout 60] | pause NAME | resume NAME
  resume [--stay]          no NAME: start again the VMs that were running before the PC restarted
  autostart [on|off]       do that at each logon (Windows task, also from WSL; systemd user unit on Linux)
  project ls | project show [P] | project notes [P [TEXT]] | project move NAME P   (sub-projects: lab/web)
  rm NAME | reset NAME     delete a stopped VM | blank its disk to reinstall
  clone SRC NEW            instant copy-on-write copy of a stopped VM (SRC then stays read-only)
  snapshot NAME save|load|rm TAG | snapshot NAME ls
  display NAME --width W --height H    screen size at the next start
  net create NET [--project P] | net rm NET | net ls | net attach|detach NAME NET   isolated VM-to-VM networks
  internet NAME [on|off]   NAT internet access (for a sandboxed VM, on = the user's approval)
  top                      running VMs' real CPU and memory on the host, allotments, quota
  daemon run|start|stop|status   supervision (started by start): a VM that stopped without a guest shutdown is
                           started again (growing delay), guest agent health, idle pause; events [-n 50] [--vm V]
  supervise NAME [--restart always|never] [--idle-pause MINUTES]   how the daemon looks after a VM
  web [--project P] [--no-open]   web dashboard (VMs; terminal-ct's containers when installed), served by the
                           daemon on 127.0.0.1 (TERMINAL_VM_WEB_PORT, default 7172) behind a token
  quota [--cpus N --memory GiB --disk GiB --vms N]   host-wide ceiling for all VMs (default from the host)
  resources NAME [--cpus N] [--memory GiB]   within the VM's ceiling (create --max-cpus/--max-memory); live where possible
  sandbox NAME [on|off]   no network, no guest agent or file copy, disk reset at each stop, actions logged
  layout NAME [us|fr]      keyboard layout for type/key (us = QWERTY, fr = AZERTY); action ... --layout: one call
  image [--os OS] [--plan]             download the installation image only
  push NAME LOCAL GUEST | pull NAME GUEST LOCAL   files, through qemu-guest-agent
  view NAME [--read-only]  terminal viewer (Kitty or Sixel: Windows Terminal, foot); Ctrl+] exits, Ctrl+O window
  show NAME [--read-only] | hide NAME   native window (VNC viewer)
  action NAME screenshot|click|move|drag|scroll|type|key|wait ...
  mcp [NAME] [--project P]   stdio MCP server (with --project: only that project and its sub-projects)
  skills get | skills path | skills install [--dir DIR]
"""
import argparse, base64, collections, contextlib, hashlib, http.server, io, json, os, platform, queue, re, secrets, shlex, shutil, signal, \
    socket, socketserver, struct, subprocess, sys, threading, time, urllib.parse, urllib.request, webbrowser
import ipaddress, ssl
from xml.sax.saxutils import escape

WINDOWS = os.name == "nt"
if not WINDOWS:
    import fcntl, termios, tty  # terminal viewer only

VERSION = "0.6.0"
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
        try:
            sync = secrets.randbelow(2 ** 31)
            self._send("guest-sync", id=sync)
            while self._reply(errors=False) != sync:  # drop replies (errors too) left by earlier clients; one sync:
                pass                      # each is answered, and an extra answer would be read as the next call's
        except BaseException:
            self.sock.close()  # an open socket left queued would hold QEMU's one pending slot
            raise

    def _send(self, execute, **arguments):
        self.sock.sendall(json.dumps({"execute": execute, **({"arguments": arguments} if arguments else {})}).encode() + b"\n")

    def call(self, execute, **arguments):
        self._send(execute, **arguments)
        return self._reply()

    def _reply(self, errors=True):
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
                if errors:
                    raise OSError(reply["error"].get("desc", "guest agent error"))
                continue
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


CLOCK_OFF_NS = 250_000_000  # a guest clock further than this from the host's is stepped


def sync_clock(name, timeout=2, g=None):
    """Set a guest's clock to the host's through its agent when it is off by more than CLOCK_OFF_NS: a guest's
    clock stops while QEMU is paused (idle pause) and while the host sleeps, and a guest days behind refuses TLS,
    apt and image pulls. Returns the drift in seconds that was corrected, 0 when none; OSError without an agent."""
    own = g is None
    g = g or QGA(name, timeout)
    try:
        t = g.call("guest-get-time")
        now = time.time_ns()
        if not isinstance(t, int) or abs(now - t) <= CLOCK_OFF_NS:
            return 0
        g.call("guest-set-time", time=time.time_ns())
        return round((now - t) / 1e9, 3)
    finally:
        if own:
            with contextlib.suppress(OSError, AttributeError):
                g.sock.close()


def _sync_after_pause(name):
    """After cont: the guest's clock missed the pause. Best effort (no agent, a busy one: the daemon's next check)."""
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        drift = sync_clock(name)
        if drift:
            event("clock_set", name, f"{drift:+} s")


def _kv(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class AgentBusy(OSError):
    """No guest agent answered in time: the command never started."""


def guest_exec(name, path, args, timeout=120, stdin=None):
    """Run a program in the guest through qemu-guest-agent (root, or SYSTEM on Windows): exit code, stdout, stderr.
    stdin: bytes given to it."""
    # the agent serves one client at a time (a node's identity step holds it ~15 s right before its ready file
    # shows): wait for it, within the command's own time; nothing has run in the guest until it answers
    end = time.time() + min(timeout, 30)
    while True:
        try:
            g = QGA(name, timeout=min(5.0, max(0.5, end - time.time())))
            break
        except OSError as e:
            if time.time() >= end:
                raise AgentBusy(f"guest agent busy or silent ({e})") from None
            time.sleep(0.2)
    try:
        pid = g.call("guest-exec", path=path, arg=args, **{"capture-output": True},
                     **({"input-data": base64.b64encode(stdin).decode()} if stdin is not None else {}))["pid"]
        end, wait = time.time() + timeout, 0.01
        while time.time() < end:
            st = g.call("guest-exec-status", pid=pid)
            if st.get("exited"):
                dec = lambda k: base64.b64decode(st.get(k, "")).decode("utf-8", "replace")
                return {"exit": st.get("exitcode", 1), "stdout": dec("out-data"), "stderr": dec("err-data")}
            time.sleep(wait)
            wait = min(0.2, wait * 2)  # short commands (an engine call in a node) answer in tens of ms
        raise OSError(f"guest command timed out ({timeout} s)")
    finally:
        g.sock.close()


def guest_ps(name, script, timeout=120):
    """Run a PowerShell script in a Windows guest through qemu-guest-agent (as SYSTEM): (exit code, output)."""
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = guest_exec(name, "powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", enc], timeout)
    return r["exit"], r["stdout"]


def guest_shell(name, command, timeout=60):
    """A shell command in the guest: sh -c on Linux, PowerShell on Windows (no guest agent in a sandbox)."""
    if PROFILES[load(name).get("os", "linux")]["family"] == "windows":
        enc = base64.b64encode(command.encode("utf-16-le")).decode()
        return guest_run(name, ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], timeout)
    return guest_run(name, ["/bin/sh", "-c", command], timeout)


def guest_run(name, argv, timeout=60, stdin=None):
    """A program in the guest with its arguments, no shell (the exec command; terminal-ct's engine calls in a
    node): {exit, stdout, stderr}."""
    m = load(name)
    if m.get("sandbox"):
        raise SystemExit(f"{name} is a sandbox: no guest agent to run commands through (use the screen tools)")
    if state(name) == "stopped":
        raise SystemExit(f"{name} is not running; start it: terminal-vm start {name}")
    with in_use(name):  # a long command is a use until it ends
        try:
            return guest_exec(name, argv[0], argv[1:], timeout, stdin)
        except AgentBusy as e:
            raise SystemExit(f"{name}: {e}; nothing was run (install qemu-guest-agent in the guest if it has "
                             "none)") from None
        except OSError as e:
            if "timed out" in str(e):
                raise SystemExit(f"{name}: {e}; the command may still be running in the guest") from None
            raise SystemExit(f"{name}: no guest agent answered ({e}); install qemu-guest-agent in the guest") from None


# ---------- projects ----------
# A project groups VMs and networks (machine.json / network "project"; "default" when none) with its notes and the
# log of what agents did there, so the next session picks up where the last one stopped. Sub-projects are paths
# (lab/web); terminal-ct inside a VM of project lab can put its containers under lab/<vm> (TERMINAL_CT_PROJECT).
DEFAULT_PROJECT = os.environ.get("TERMINAL_VM_PROJECT") or "default"
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}(/[a-z0-9][a-z0-9_.-]{0,47}){0,3}")


def check_project(p):
    if not isinstance(p, str) or not _PROJECT.fullmatch(p):
        raise SystemExit(f"invalid project: {p!r} (lowercase names, sub-projects as lab/web)")
    return p


def under(p, project):
    """p is project itself or one of its sub-projects; no project means everything."""
    return not project or p == project or p.startswith(project + "/")


def project_of(m):
    return m.get("project") or "default"


def project_dir(project):
    # one flat folder per project ("+" for "/") under .projects: a dot name no VM can have
    d = os.path.join(STORE, ".projects", check_project(project or DEFAULT_PROJECT).replace("/", "+"))
    os.makedirs(d, exist_ok=True)
    return d


NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}  # a detached daemon: no console flash


# ---------- one operation per VM at a time ----------

class vm_op:
    """One operation on a VM at a time between processes (an atomic mkdir in its folder): the daemon's restart
    against the user's stop, rm, reset, clone and snapshot. Re-entrant within a thread (the daemon's web threads and
    its supervisor wait for each other like processes). wait: seconds to wait for
    it, then SystemExit."""
    held = collections.Counter()

    def __init__(self, name, wait=30):
        self.name, self.wait, self.key = name, wait, (threading.get_ident(), name)

    def __enter__(self):
        if vm_op.held[self.key]:
            vm_op.held[self.key] += 1
            return self
        path, end = os.path.join(vm_dir(self.name), ".op.lock"), time.monotonic() + self.wait
        while True:
            try:
                os.mkdir(path)
                break
            except FileNotFoundError:  # no such VM (folder gone): nothing to guard
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if time.time() - os.path.getmtime(path) > 3600:  # left by a killed command
                        stale = f"{path}.{os.getpid()}"
                        os.rename(path, stale)  # of two seeing it stale, one claims it
                        os.rmdir(stale)
                        continue
                if time.monotonic() >= end:
                    raise SystemExit(f"{self.name} is busy with another terminal-vm operation; try again")
                time.sleep(0.2)
        vm_op.held[self.key] += 1
        return self

    def __exit__(self, *exc):
        vm_op.held[self.key] -= 1
        if not vm_op.held[self.key]:
            del vm_op.held[self.key]  # one key per web thread: none left behind
            with contextlib.suppress(OSError):
                os.rmdir(os.path.join(vm_dir(self.name), ".op.lock"))


def locked_op(f):
    """f(name, ...) run under vm_op(name)."""
    def run(name, *a, **k):
        with vm_op(name):
            return f(name, *a, **k)
    run.__name__, run.__doc__ = f.__name__, f.__doc__
    return run


def disk_in_use(name):
    """True while a QEMU runs this VM, proof that does not depend on QMP answering (one client at a time): the
    disk's image lock; on Windows, where QEMU takes no file locks, the process qemu.pid names, if it was created
    before that file was written (a pid left by a crash or a reboot may name another process now)."""
    disk = os.path.join(vm_dir(name), "disk.qcow2")
    if WINDOWS:
        pid = _pid(name)
        born = _proc_created(pid) if pid else None
        try:
            return born is not None and born <= os.path.getmtime(os.path.join(vm_dir(name), "qemu.pid")) + 5
        except OSError:
            return False
    if not os.path.exists(disk):
        return False
    r = subprocess.run([qemu_bin("qemu-img"), "info", disk], capture_output=True, text=True, **NO_WINDOW)
    return r.returncode != 0 and "lock" in (r.stderr or "").lower()  # a corrupt disk or no qemu-img is no proof


def _proc_created(pid):
    """Windows: when the process was created (epoch seconds), or None if it is gone."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    h = k.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            return None
        return ((t[0].dwHighDateTime << 32 | t[0].dwLowDateTime) - 116444736000000000) / 1e7
    finally:
        k.CloseHandle(h)


@contextlib.contextmanager
def in_use(name):
    """A long use (exec, file copy): its last use kept fresh every 30 s while it lasts, so no idle pause hits it
    halfway; a VM the daemon paused is woken first."""
    with contextlib.suppress(RuntimeError, OSError):  # QMP busy: the operation itself says what is wrong
        wake(name)
    done = threading.Event()

    def keep():
        while not done.wait(30):
            touch_use(name)
    threading.Thread(target=keep, daemon=True).start()
    try:
        yield
    finally:
        done.set()
        touch_use(name)


# ---------- resume after a restart ----------
# What each VM should be doing (vm_dir/want: running or stopped, written by start and stop), so after the PC (or
# WSL) restarts, `resume` starts again what was running and leaves alone what was stopped on purpose.

def watch(name):
    """Started by start, until the VM ends: a shutdown from inside the guest (QMP SHUTDOWN, guest: true) means it is
    meant to stay off, so resume after a restart leaves it so. A host signal or a power cut is not a guest shutdown:
    such a VM comes back."""
    for _ in range(50):
        try:
            q = QMP(endpoint(name, "qmp-watch"))
            break
        except RuntimeError:
            time.sleep(0.2)
    else:
        return
    try:
        q.sock.settimeout(None)
        while True:
            msg = q._recv()
            if msg.get("event") == "SHUTDOWN" and (msg.get("data") or {}).get("guest"):
                want(name, "stopped")
                return
    except (OSError, RuntimeError, ValueError):  # QEMU gone
        return
    finally:
        q.close()


def want(name, value):
    with open(os.path.join(vm_dir(name), "want"), "w") as f:
        f.write(value)


def wanted(name):
    try:
        with open(os.path.join(vm_dir(name), "want")) as f:
            return f.read().strip()
    except OSError:
        return "stopped"


# Rename a Windows clone without rebooting it (tested on Server 2025): the registry names (hostname, DNS, .NET read
# them live), WMI restarted (it caches the name), NetBIOS turned off (its driver keeps the boot-time name, which
# would clash with the source's on a shared network; names still resolve through DNS/LLMNR), then the user's session
# logged off: auto-logon brings it back (~20 s) with the new %COMPUTERNAME%.
RENAME_PS = r"""$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $n='{name}'
$k='HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName'; $t='HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters'
Set-ItemProperty "$k\ComputerName" ComputerName $n; Set-ItemProperty "$k\ActiveComputerName" ComputerName $n
Set-ItemProperty $t Hostname $n; Set-ItemProperty $t 'NV Hostname' $n
Restart-Service winmgmt -Force
Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=true' |
  % {{ Invoke-CimMethod -InputObject $_ -MethodName SetTcpipNetbios -Arguments @{{TcpipNetbiosOptions=[uint32]2}} | Out-Null }}
Get-Process explorer -ErrorAction SilentlyContinue | Select-Object -First 1 | % {{ logoff $_.SessionId }}
"""


def computer_name(vm):
    """A valid Windows computer name from a VM name: 15 characters, letters, digits and hyphens, not all digits."""
    n = re.sub(r"[^A-Za-z0-9-]", "-", vm)[:15].strip("-").upper()
    return n if n and not n.isdigit() else ("VM-" + n)[:15].strip("-")


def rename_guest(name, wait=600):
    """Started by start for a Windows clone: once the guest agent answers, give the guest its own name."""
    end = time.time() + wait
    while time.time() < end and state(name) != "stopped":
        try:
            code, out = guest_ps(name, RENAME_PS.format(name=load(name)["rename"]))
        except OSError:  # agent not up yet
            time.sleep(3); continue
        if code == 0:
            m = load(name); m.pop("rename", None); save(name, m)
        return


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


def shot_pixels(name):
    q = qmp(name)
    try:
        return screen_pixels(name, q)[0]
    finally:
        q.close()


def settled_screenshot(name, before=None, timeout=1.5):
    """PNG after an action: once the screen differs from `before` (a loaded guest shows keystrokes late),
    at most `timeout` s, then 0.3 s for the redraw to finish."""
    q = qmp(name)
    try:
        end = time.time() + timeout
        while before is not None and time.time() < end and screen_pixels(name, q)[0] == before:
            time.sleep(0.1)
        time.sleep(0.3)
        return screenshot(name, q)[0]
    finally:
        q.close()


def act(name, tool, a):
    """Shared by the CLI and MCP. Coordinates are pixels of the latest screenshot."""
    wake(name)
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
                                            'powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0',
                                            # Server Manager opened full screen at every logon, over the desktop
                                            'reg add HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\Server\\ServerManager '
                                            '/v DoNotOpenAtLogon /t REG_DWORD /d 1 /f'), 1))
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
            '<settings pass="specialize">' + comp(shell, f"<ComputerName>*</ComputerName><TimeZone>{escape(host_timezone())}</TimeZone>")
            + comp("Microsoft-Windows-Deployment", f"<RunSynchronous>{machine}</RunSynchronous>") + '</settings>'
            '<settings pass="oobeSystem">' + comp("Microsoft-Windows-International-Core", intl())
            # AdministratorPassword: Server OOBE otherwise stops on the built-in Administrator's password page
            + comp(shell, f'<UserAccounts><AdministratorPassword><Value>{escape(password)}</Value><PlainText>true</PlainText>'
                          f'</AdministratorPassword><LocalAccounts><LocalAccount wcm:action="add">{pw}<Name>{escape(user)}</Name>'
                          f'<Group>Administrators</Group></LocalAccount></LocalAccounts></UserAccounts>'
                          f'<AutoLogon>{pw}<Enabled>true</Enabled><LogonCount>9999</LogonCount><Username>{escape(user)}</Username></AutoLogon>'
                          + oobe + first) + '</settings></unattend>\n')


def host_timezone():
    """The host's Windows time zone ID (tzutil, also reachable from WSL), so the guest's clock matches.
    shortcut: other Linux hosts get UTC (no IANA to Windows table); with -rtc base=localtime the clock still shows host time."""
    try:
        tz = subprocess.run(["tzutil" if WINDOWS else "/mnt/c/Windows/System32/tzutil.exe", "/g"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # best effort: any failure means UTC
        tz = ""
    return tz if re.fullmatch(r"[\w .+()-]{1,64}", tz or "") else "UTC"


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
            *(["-rtc", "base=localtime"] if m.get("rtc_local") else []),  # Windows reads the RTC as local time
            # QEMU locks the image: one VM per disk. During an unattended install, ignore guest flushes (much faster):
            # a host crash then loses the install, never a finished VM.
            "-drive", f"file={os.path.join(d, 'disk.qcow2')},if={p['disk_bus']}" + (",cache=unsafe" if setup else "")
            + (",snapshot=on" if sandbox and not setup else ""),
            *(["-cdrom", m["iso"], "-boot", "order=cd"] if m.get("iso") else []), "-device", vga,
            *(["-drive", f"if=virtio,driver=vvfat,dir={os.path.join(d, 'cidata').replace(',', ',,')},label=cidata,rw=off"]
              if m.get("cloud") else []),
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
            "-qmp", ep("qmp-watch") + ",server=on,wait=off",  # watch(): tells a shutdown from inside the guest
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
           offline=False, product_key=None, ui_language="en-US", sandbox=False, max_cpus=None, max_memory=None,
           project=None, image=None, node=False, template=True, base_image=None):
    """A VM installed from an ISO, or (image) one booting an official cloud image, set up by cloud-init at its
    first start; node: with Docker and Python 3, for terminal-ct, an overlay on the node template (built once;
    template=False: from the cloud image itself, as the template's own build)."""
    d = vm_dir(name)
    project = check_project(project or DEFAULT_PROJECT)
    p = PROFILES.get(os_name)
    if p is None:
        raise SystemExit(f"unknown --os {os_name!r}; choose from {', '.join(PROFILES)}")
    cpus, memory, disk = cpus or min(p["cpus"], os.cpu_count()), memory or p["memory"], disk or p["disk"]
    if os.path.exists(os.path.join(d, "machine.json")):
        raise SystemExit(f"VM {name!r} already exists in {d}")
    if image and (image not in CLOUD_IMAGES or p["family"] != "linux" or sandbox):
        raise SystemExit(f"--image: one of {', '.join(CLOUD_IMAGES)}, for a Linux VM (not a sandbox)")
    if node and not image:
        raise SystemExit("--node needs --image (debian or alpine)")
    if not image and (not iso or not os.path.isfile(iso)):
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
    tpl = bool(node and template)
    if tpl:
        with quota_lock():  # said before a template build of minutes, not after (checked again below)
            quota_check({"disk_gb": disk})
    # before the folder exists: a failed download (or template build) leaves nothing
    base = node_template(image) if tpl else (base_image or cloud_image(image)) if image else None
    with quota_lock():
        quota_check({"disk_gb": disk})
        os.makedirs(d, mode=0o700, exist_ok=True)  # under the lock: the disk now counts for the next check
        with open(os.path.join(d, "machine.json"), "w") as f:
            json.dump({"os": os_name, "memory_mb": memory * 1024, "cpus": cpus, "disk_gb": disk}, f)
    subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", *(["-F", "qcow2", "-b", base] if base else []),
                    os.path.join(d, "disk.qcow2"), f"{disk}G"], check=True, stdout=subprocess.DEVNULL)
    m = {"os": os_name, **({"iso": os.path.abspath(iso)} if not image else
                           {"image": image, "installed": True, **({"node": True} if node else {}),
                            **({"template": base} if tpl else {"cloud": True})}),
         "memory_mb": memory * 1024, "cpus": cpus, "width": width,
         "height": height, "layout": layout, "offline": offline or sandbox, **({"sandbox": True} if sandbox else {}),
         "max_cpus": top[0], "max_memory_mb": top[1] * 1024, "disk_gb": disk, "project": project,
         **({"balloon": True} if p["family"] == "linux" else {})}
    if p["family"] == "windows":
        # 12 characters: within XP's 14-character limit, and the suffix meets Server's complexity policy
        # (an unmet policy would stop the unattended install at a password prompt).
        m.update(user="Administrator" if p["unattend"] == "sif" else "user",
                 password=secrets.token_urlsafe(6) + "-Aa1")
        write_answers(d, m, key, ui_language)
    if image:  # the account cloud-init makes; the guest agent is how terminal-vm and terminal-ct reach it
        m.update(user="user", password=secrets.token_urlsafe(9))
        node_seed(name, m) if tpl else cloud_seed(name, m)
    with open(os.path.join(d, "machine.json"), "w") as f:
        json.dump(m, f)
    out = {"created": name, "os": os_name, "path": d, "project": project}
    if image:
        out.update(image=image, user=m["user"], password=m["password"], ready_file=m["ready_file"],
                   note=("from the node template: ready about 15 s after its first start" if tpl else
                         "set up by cloud-init at its first start (1-5 min, needs the internet)")
                   + "; ready when ready_file exists in the guest")
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
    # host time zone in the answers, local-time RTC: only for installs from these answers (an older VM's clock
    # was set for a UTC RTC)
    m["rtc_local"] = True
    tools = guest_tools() if m["os"] in GUEST_TOOLS_OS else None
    if tools:  # installed at first logon from the answers drive (autounattend: GUEST_TOOLS_CMD)
        shutil.copyfile(tools, os.path.join(d, "answers", "virtio-win-guest-tools.exe"))
        m["balloon"] = True  # the tools include the balloon driver


@locked_op
def start(name, window=False, set_want=True):
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
    if set_want:  # the daemon's restart leaves it as it is: a stop typed meanwhile wins
        want(name, "running")
    if os.environ.get("TERMINAL_VM_DAEMON", "1") != "0":  # 0: no supervision (tests, or by choice)
        with contextlib.suppress(Exception):
            daemon("start")


def _start(name, window=False):
    d, m = vm_dir(name), load(name)
    if state(name) != "stopped" or disk_in_use(name):  # the second: QEMU alive behind a busy QMP
        raise SystemExit(f"{name} is already running")
    for f, on in (("window", window), ("idle_paused", False)):  # window: closing it is the user's way to stop it
        with contextlib.suppress(OSError):
            open(os.path.join(d, f), "w").close() if on else os.remove(os.path.join(d, f))
    if len(os.path.join(d, "qmp-view.sock").encode()) >= 108:  # sun_path limit
        raise SystemExit(f"VM folder path too long for its sockets: {d}; shorten the name or set TERMINAL_VM_HOME")
    vnc_port = None
    if WINDOWS:
        # QEMU takes a display number (port - 5900); Windows' ephemeral range can start at 1024
        vnc_port = next(p for p in iter(lambda: free_ports(1)[0], None) if p >= 5900)
        with open(os.path.join(d, "ports.json"), "w") as f:
            json.dump({"vnc": vnc_port}, f)
    for k in ("qmp", "qmp-view", "qmp-watch", "qga", "vnc"):  # stale sockets from a crashed VM
        if os.path.exists(os.path.join(d, k + ".sock")):
            os.unlink(os.path.join(d, k + ".sock"))
    for f in os.listdir(d):  # raw frames left by viewers that were killed (2-10 MB each)
        if f.startswith("view") and f.endswith(".ppm"):
            os.remove(os.path.join(d, f))
    log = open(os.path.join(d, "qemu.log"), "ab")
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if WINDOWS
              else {"start_new_session": True})
    if m.get("rename"):  # a Windows clone's first run: its own computer name (it was the source's)
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_rename", name], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=log, **detach)
    if m.get("identity"):  # a template node's first run: its own name, account and disk size
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_identity", name], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=log, **detach)
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
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "_watch", name], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=log, **detach)
            print(json.dumps({"started": name})); return
        time.sleep(0.1)
    raise SystemExit(f"QEMU did not start; see {os.path.join(d, 'qemu.log')}")


@locked_op
def stop(name, force=False, timeout=60):
    load(name)
    touch_use(name)  # no idle pause in the middle of a shutdown
    want(name, "stopped")  # first: stopped on purpose, resume leaves it so
    if not force and os.path.exists(_idle_marker(name)):  # paused by the daemon: a paused guest ignores the button
        with contextlib.suppress(RuntimeError, OSError):
            wake(name)
    if state(name) == "stopped":  # a stale qemu.pid may name an unrelated process now
        if disk_in_use(name):  # QMP busy, QEMU alive
            raise SystemExit(f"{name}: QEMU is busy (another command holds its monitor); try again in a moment")
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
    with contextlib.suppress(OSError):  # the user's own pause or resume: no longer the daemon's idle pause
        os.remove(_idle_marker(name))
    q = qmp(name)
    q.cmd("cont" if resume else "stop")
    q.close()
    if resume:
        _sync_after_pause(name)
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
    if cmd == "mcp" and "--project" in rest:
        return None  # projects are terminal-vm's: terminal-mac's server has none (its VMs are out of such a scope)
    if cmd in ("up", "create", "image") and "--os" not in rest:
        return argv  # a Mac's own guest by default, as terminal-mac always did (x86 guests would be emulated)
    if cmd not in NAMED or cmd == "resume" and not [a for a in rest if not a.startswith("-")]:
        return None  # resume without a name: every VM that ran before a restart, not a macOS VM's un-pause
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
    try:
        r = subprocess.run([exe, "ls"], capture_output=True, text=True, timeout=10)
        machines = json.loads(r.stdout) if r.returncode == 0 else []
    except (ValueError, subprocess.TimeoutExpired):
        return []
    machines = [m for m in machines if isinstance(m, dict) and m.get("name")]
    return [{"name": m["name"], "os": "macos", "engine": "terminal-mac", "cpus": m.get("cpu"),
             "memory_gb": m.get("memoryGiB"), "display": f"{m.get('width')}x{m.get('height')}"} for m in machines]


# ---------- supervision: the daemon ----------
# `terminal-vm daemon` (started by `start`): every SUP_TICK seconds it starts again a VM that should run (want:
# running) but stopped without a guest shutdown (QEMU crashed or was killed; confirmed by the disk's lock, not only
# by QMP, which answers one client at a time), with a growing delay; pings the guest agent of running VMs (health);
# pauses a VM nobody used for its `idle_pause` minutes (no screen action, exec, file copy, view, show or VNC
# client) that is not computing either (QEMU under IDLE_CPU), and the next use wakes it. What it does goes to
# STORE/events.log (terminal-ct's line format, `vm` for `container`). It ends by itself once no VM has wanted to run
# for IDLE_EXIT seconds.
SUP_TICK = 5
SUP_BACKOFF_MAX = 300
SUP_STABLE = 600  # up this long: the next crash starts the delays from the beginning again
HEALTH_GRACE = 300  # no health verdict in a VM's first minutes (Windows boots slowly)
HEALTH_RETRIES = 3
IDLE_CPU = 20.0  # % of one core: an idle guest still costs QEMU 4-12 % (measured: TinyCore in WSL); a job more
IDLE_EXIT = 600


def event(kind, vm=None, detail=None):
    e = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": kind,
         **{k: v for k, v in (("vm", vm), ("detail", detail)) if v}}
    path = os.path.join(STORE, "events.log")
    with contextlib.suppress(OSError):
        os.makedirs(STORE, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")
        if os.path.getsize(path) > 4 * 2 ** 20:  # a daemon runs for months: keep the latest lines
            with open(path, encoding="utf-8") as f:
                keep = f.readlines()[-10000:]
            tmp = f"{path}.{os.getpid()}.new"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                os.replace(tmp, path)  # Windows: refused while another process appends; trimmed next time
            finally:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
    return e


def events(n=50, vm=None, project=None):
    try:
        with open(os.path.join(STORE, "events.log"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []
    mine = None if project is None else {v for v in machines() if under(project_of(load(v)), project)}
    return [e for e in rows if (vm is None or e.get("vm") == vm) and (mine is None or e.get("vm") in mine)][-n:]


def _idle_marker(name):
    return os.path.join(vm_dir(name), "idle_paused")


def touch_use(name):
    with contextlib.suppress(OSError, SystemExit):
        open(os.path.join(vm_dir(name), "last_use"), "w").close()


def wake(name):
    """Called on every use of a VM: records it (idle_pause counts from the last use), and runs a VM the daemon
    paused for idleness again (a pause by the user stays). The marker goes only once QEMU runs it again: a busy QMP
    leaves it for the next use to retry."""
    touch_use(name)
    if not os.path.exists(_idle_marker(name)):
        return
    q = qmp(name)
    try:
        woke = q.cmd("query-status")["status"] == "paused"
        if woke:
            q.cmd("cont")
            event("woken", name)
    finally:
        q.close()
    with contextlib.suppress(OSError):
        os.remove(_idle_marker(name))
    if woke:
        _sync_after_pause(name)


def supervise(name, restart=None, idle_pause=None):
    """Show, or set, how the daemon looks after a VM: restart always|never; idle_pause minutes (0: never)."""
    m = load(name)
    if restart is not None:
        if restart not in ("always", "never"):
            raise SystemExit("restart is always or never")
        m["restart"] = restart
    if idle_pause is not None:
        if idle_pause < 0:
            raise SystemExit("idle_pause is a number of minutes (0: never)")
        m["idle_pause"] = idle_pause
    if restart is not None or idle_pause is not None:
        save(name, m)
    return {"name": name, "restart": m.get("restart", "always"), "idle_pause": m.get("idle_pause", 0)}


def _sup_file():
    return os.path.join(STORE, ".supervisor.json")


def _sup_load():
    try:
        with open(_sup_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _sup_save(st):
    tmp = f"{_sup_file()}.{os.getpid()}.new"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, _sup_file())


def _pid(name):
    try:
        with open(os.path.join(vm_dir(name), "qemu.pid")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _restart(n, s, now):
    """A VM that should run is down: start it again, unless a window was closed (wanted: off), the VM is busy with a
    user operation, or its disk is still held (QEMU alive, QMP only busy)."""
    if os.path.exists(os.path.join(vm_dir(n), "window")):  # QEMU's window closed: the user's way to stop it
        os.remove(os.path.join(vm_dir(n), "window"))
        want(n, "stopped")
        return [event("window_closed", n, "its window was closed: left stopped")]
    try:
        with vm_op(n, wait=0):
            if wanted(n) != "running" or disk_in_use(n):  # changed meanwhile, or QEMU alive behind a busy QMP
                return []
            s["restarts"] = s.get("restarts", 0) + 1
            s["next_try"] = now + min(SUP_BACKOFF_MAX, 5 * 2 ** min(s.get("streak", 0), 6))
            s["streak"] = s.get("streak", 0) + 1
            try:
                quiet(start, n, set_want=False)
            except (SystemExit, Exception) as e:  # quota, a missing disk: try again later
                return [event("restart_failed", n, str(e))]
            return [event("restarted", n, f"restart {s['restarts']} (it stopped without a guest shutdown)")]
    except SystemExit:  # a user operation holds it: next pass
        return []


def supervise_pass(st, now=None, cpu=None):
    """One pass over the VMs; st: the daemon's memory (saved to STORE/.supervisor.json), cpu: {name: (time, CPU s)}
    kept in the daemon's process between passes. Returns the events it caused. One VM's failure (a busy QMP) never
    stops the pass for the others."""
    now, cpu, done = time.time() if now is None else now, cpu if cpu is not None else {}, []
    for n in machines():
        s = st.setdefault(n, {})
        try:
            done += _supervise_one(n, s, now, cpu)
            s.pop("error", None)
        except (SystemExit, Exception) as e:
            if s.get("error") != str(e):  # once per distinct error, not every pass
                done.append(event("supervise_failed", n, str(e)))
            s["error"] = str(e)
    for gone in set(st) - set(machines()):
        st.pop(gone)
    return done


def _supervise_one(n, s, now, cpu):
    m, st_ = load(n), state(n)
    if st_ == "stopped":
        candidate = wanted(n) == "running" and m.get("restart", "always") != "never"
        if candidate and disk_in_use(n):  # QMP busy or not answering, QEMU alive: unknown, judge nothing
            return []
        cpu.pop(n, None)
        for k in ("since", "health_fails", "agent", "idle_since"):
            s.pop(k, None)
        return _restart(n, s, now) if candidate and now >= s.get("next_try", 0) else []
    if "since" not in s:
        s["since"] = now
    if now - s["since"] > SUP_STABLE:
        s["streak"] = 0
    if st_ != "running":  # paused: by the user, or idle (woken by the next use)
        cpu.pop(n, None)
        return []
    done = []
    try:
        used = os.path.getmtime(os.path.join(vm_dir(n), "last_use"))
    except OSError:
        used = 0
    # health: judged only once the guest agent has answered (VMs without one are not), after a grace period, and
    # not right after a use (exec and file copies hold the agent's one connection); a short wait until it answered
    if now - used > 60:
        try:
            g = QGA(n, timeout=2 if s.get("agent") else 0.5)
            g.call("guest-ping")
            if now - s.get("clock_at", 0) >= 60:  # once a minute: catches a host that slept (no event for it)
                s["clock_at"] = now
                with contextlib.suppress(OSError, RuntimeError, ValueError):  # a guest that cannot set it: health
                    drift = sync_clock(n, g=g)                                # still judged by the ping alone
                    if drift:
                        done.append(event("clock_set", n, f"{drift:+} s"))
            if s.get("health_fails", 0) >= HEALTH_RETRIES:
                done.append(event("healthy", n))
            s["agent"], s["health_fails"] = True, 0
        except (OSError, RuntimeError, ValueError):
            if s.get("agent") and now - s["since"] > HEALTH_GRACE:
                s["health_fails"] = s.get("health_fails", 0) + 1
                if s["health_fails"] == HEALTH_RETRIES:
                    done.append(event("unhealthy", n, f"guest agent silent {HEALTH_RETRIES} times in a row"))
    # idle pause: QEMU's CPU % between two passes, the last use, VNC clients
    minutes, pid = m.get("idle_pause", 0), _pid(n)
    t = _proc_times(pid) if minutes and pid else None
    if not t:
        return done
    last, cpu[n] = cpu.get(n), (now, t[0])
    if not last or now <= last[0]:
        return done
    if (t[0] - last[1]) / (now - last[0]) * 100 >= IDLE_CPU:  # computing: not idle, whoever uses it
        s.pop("idle_since", None)
        return done
    if now - max(s.setdefault("idle_since", now), used) < minutes * 60:
        return done
    q = qmp(n)
    try:
        if q.cmd("query-vnc").get("clients"):  # someone watches through a VNC viewer (show): in use
            s["idle_since"] = now
            return done
        with contextlib.suppress(OSError):  # a use since the start of this pass wins
            if now - os.path.getmtime(os.path.join(vm_dir(n), "last_use")) < minutes * 60:
                return done
        open(_idle_marker(n), "w").close()  # first: a use from now on finds it and wakes the VM
        try:
            q.cmd("stop")
        except Exception:
            os.remove(_idle_marker(n))
            raise
    finally:
        q.close()
    s.pop("idle_since", None)
    return done + [event("idle_paused", n, f"unused and under {IDLE_CPU:g}% CPU for {minutes} min; woken by the next use")]


def _daemon_file(kind):
    return os.path.join(STORE, f"daemon.{kind}")


def daemon_alive():
    try:
        with open(_daemon_file("beat")) as f:
            return time.time() - float(f.read().split()[0]) < 5 * SUP_TICK
    except (OSError, ValueError, IndexError):
        return False


def _claim_daemon_lock():
    """The daemon's lock (a folder). Its owner beats (daemon.beat) from the start; a lock with no beat for
    2 * SUP_TICK that is older than 30 s was left by a daemon that died (a pid alone proves nothing after a reboot:
    numbers are reused), and is taken over by rename, so of two takers only one wins."""
    lock = os.path.join(STORE, ".daemon.lock")
    os.makedirs(STORE, exist_ok=True)
    for _ in range(2):
        try:
            os.mkdir(lock)
            return lock
        except FileExistsError:
            for _ in range(4 * SUP_TICK):
                if daemon_alive():
                    return None  # one daemon per store
                time.sleep(0.5)
            try:
                if time.time() - os.path.getmtime(lock) < 30:  # a daemon starting right now
                    return None
            except OSError:
                continue  # removed meanwhile: try mkdir again
            stale = f"{lock}.stale-{os.getpid()}"
            try:
                os.rename(lock, stale)
            except OSError:
                return None  # another taker won
            shutil.rmtree(stale, ignore_errors=True)
    return None


def daemon(cmd="status"):
    if cmd == "status":
        return {"daemon": "running" if daemon_alive() else "stopped", "supervised": [
            dict(supervise(n), want=wanted(n), status=state(n)) for n in machines()]}
    if cmd == "stop":
        if daemon_alive():
            open(_daemon_file("stop"), "w").close()
            for _ in range(6 * SUP_TICK):
                if not daemon_alive():
                    break
                time.sleep(0.5)
        return {"daemon": "running" if daemon_alive() else "stopped"}
    if cmd == "start":
        if daemon_alive():
            return {"daemon": "running"}
        with contextlib.suppress(OSError):
            os.remove(_daemon_file("stop"))
        os.makedirs(STORE, exist_ok=True)
        args = [sys.executable, os.path.abspath(__file__), "daemon", "run"]
        with open(_daemon_file("log"), "ab") as log:  # the child keeps its own handle
            if WINDOWS:  # out of the parent's job too: else it dies with the terminal or MCP server that started it
                base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                try:
                    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     creationflags=base | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
                except OSError:  # a job that forbids breaking away
                    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=base)
            else:
                subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return {"daemon": "started"}
    if cmd != "run":
        raise SystemExit("daemon run|start|stop|status")
    lock = _claim_daemon_lock()
    if not lock:
        return {"daemon": "running"}
    alive = threading.Event()

    def beat():  # its own thread: a slow pass (QMP timeouts on several VMs) must not make the daemon look dead
        while not alive.is_set():
            with contextlib.suppress(OSError):
                tmp = f"{_daemon_file('beat')}.new"
                with open(tmp, "w") as f:
                    f.write(f"{time.time()} {os.getpid()}")
                os.replace(tmp, _daemon_file("beat"))
            alive.wait(SUP_TICK)
    try:
        threading.Thread(target=beat, daemon=True).start()
        threading.Thread(target=web_serve, daemon=True).start()
        event("daemon_started", detail=f"pid {os.getpid()}")
        st, cpu, quiet_since, last_err = _sup_load(), {}, time.time(), None
        while not os.path.exists(_daemon_file("stop")):
            try:
                supervise_pass(st, cpu=cpu)
                _sup_save(st)
                last_err = None
            except Exception as e:  # one bad pass must not end supervision; logged once, not every 5 s
                if str(e) != last_err:
                    print(f"{time.strftime('%H:%M:%S')} pass: {e}", file=sys.stderr, flush=True)
                last_err = str(e)
            if any(wanted(n) == "running" for n in machines()) or time.time() - WEB_LAST[0] < IDLE_EXIT:
                quiet_since = time.time()  # VMs to look after, or a dashboard open
            elif time.time() - quiet_since > IDLE_EXIT:
                alive.set()  # look stopped first: a start from now on launches a new daemon...
                with contextlib.suppress(OSError):
                    os.remove(_daemon_file("beat"))
                if any(wanted(n) == "running" for n in machines()):  # ...or one came in just before: stay
                    alive.clear()
                    threading.Thread(target=beat, daemon=True).start()
                    quiet_since = time.time()
                    continue
                event("daemon_stopped", detail="no VM to look after")
                break
            time.sleep(SUP_TICK)
        else:
            event("daemon_stopped")
    finally:
        alive.set()
        for k in ("stop", "beat", "web"):
            with contextlib.suppress(OSError):
                os.remove(_daemon_file(k))
        shutil.rmtree(lock, ignore_errors=True)


# ---------- web dashboard: terminal-vm's side (VMs here, terminal-ct's containers through its command line) ----------
TOOL = "terminal-vm"
WEB_PORT = int(os.environ.get("TERMINAL_VM_WEB_PORT") or 7172)  # terminal-ct's is 7171: both can run
_CT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")  # terminal-ct's container, stack and service names


def ct_cli():
    return peer_cli("TERMINAL_CT", "terminal-ct")


def ct_call(args, timeout=60, **k):
    cli = ct_cli()
    if not cli:
        raise RuntimeError("terminal-ct not found: put it on the PATH, or set TERMINAL_CT")
    return cli_call(cli, args, timeout, **k)


def _ct_name(n):
    if not isinstance(n, str) or not _CT_NAME.fullmatch(n):
        raise ValueError(f"invalid container or stack name {n!r}")
    return n


def _vm_rows(project=None):
    """ls's rows of this store's VMs in the project, with the real use of the running ones (top)."""
    rows = [v for v in vm_list() + mac_machines() if under(v.get("project") or DEFAULT_PROJECT, project)]
    if any(v.get("status", "stopped") != "stopped" for v in rows):  # terminal-mac's: shown, run by it
        use = {v["name"]: v for v in top().get("vms", [])}
        for v in rows:
            v.update({k: use.get(v["name"], {}).get(k) for k in ("cpu_percent", "host_memory_gb")})
    return rows


_CT_CACHE = {}  # project: (time, reply): several tabs, one terminal-ct call every few seconds


def ct_dash(project=None, max_age=2):
    hit = _CT_CACHE.get(project)
    if hit and time.time() - hit[0] < max_age:
        return dict(hit[1])
    d = ct_call(["dash", "--json"] + (["--project", project] if project else []), timeout=30)
    _CT_CACHE[project] = (time.time(), d)
    return dict(d)


def web_api(method, path, q, body=None):
    """One dashboard call: (content type, bytes or a JSON-able value). Raises on a bad request."""
    project = q.get("project") or None
    if project:
        check_project(project)
    if method == "GET" and path == "/api/state":  # containers: terminal-ct's own reply, if it is installed
        d = {"engine": None, "daemon": daemon_alive(), "project": project, "rows": [], "stacks": [], "events": []}
        if not ct_cli():
            d["note"] = "terminal-ct not found (PATH or TERMINAL_CT): no containers shown"
        else:
            try:
                d = ct_dash(project)
                if not d.get("daemon") and d.get("stacks"):  # its stacks are not kept while its daemon is down
                    d["note"] = "terminal-ct's daemon is stopped: its stacks are not kept (terminal-ct daemon start)"
            except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                d["note"] = f"terminal-ct: {e}"
        d["tool"], d["daemon"] = TOOL, daemon_alive()  # the header shows the daemon serving this page
        return "application/json", d
    if method == "GET" and path == "/api/vms":
        return "application/json", {"vms": _vm_rows(project), "events": events(8, project=project)}
    if method == "GET" and path == "/api/quota":
        return "application/json", {"quota": quota(), "projects": [p["project"] for p in projects()]}
    if method == "GET" and path == "/api/shot":
        name = q.get("name", "")
        if q.get("kind") == "vm":  # not a use: watching thumbnails must not keep a VM from its idle pause
            qm, f = qmp(name), f"web{threading.get_ident()}.png"  # QMP takes one client: closed at once
            try:
                return "image/png", screenshot(name, qm, f)[0]
            finally:
                qm.close()
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(vm_dir(name), f))
        os.makedirs(os.path.join(STORE, "web"), mode=0o700, exist_ok=True)  # screenshots, stacks (env values)
        out = os.path.join(STORE, "web", f"ct-{_ct_name(name)}.{threading.get_ident()}.png")
        try:
            ct_call(["action", name, "screenshot", "--output", out], timeout=20)
            with open(out, "rb") as f:
                return "image/png", f.read()
        finally:
            with contextlib.suppress(OSError):
                os.remove(out)
    if method == "GET" and path == "/api/logs":
        n = str(max(1, min(int(q.get("n") or 200), 10000)))
        return "text/plain; charset=utf-8", ct_call(["logs", _ct_name(q.get("name")), "-n", n], raw=True).encode()
    if method == "GET" and path == "/api/stack":
        st = ct_call(["stack", "show", _ct_name(q.get("name"))])
        services = {k: {a: b for a, b in v.items() if a != "running"} for k, v in st.get("services", {}).items()}
        return "application/json", {"desired": {"stack": st["stack"], "project": st.get("project"), "services": services},
                                    "status": st}
    if method == "POST" and path == "/api/do":
        if not isinstance(body, dict):
            raise ValueError("a JSON object")
        op, name = body.get("op"), body.get("name") or ""
        if body.get("kind") == "vm":
            if op not in ("start", "stop", "force_stop"):
                raise ValueError("VMs: start, stop or force_stop")
            vm_dir(name)  # a valid VM name
            if op == "start":
                return "application/json", quiet(start, name)
            try:
                return "application/json", quiet(stop, name, force=op == "force_stop")
            except SystemExit as e:
                if e.code == 1:  # stop printed why on stderr: the guest ignored the power button
                    raise RuntimeError(f"{name}: the guest did not shut down within 60 s (no ACPI handling, or a "
                                       f"dialog is open); take a look, or force stop") from None
                raise
        _CT_CACHE.clear()  # the next refresh shows the container's new state at once
        try:
            if op in ("start", "stop", "rm"):
                return "application/json", ct_call([op, _ct_name(name)], timeout=120)
            if op == "restart":
                ct_call(["stop", _ct_name(name)], timeout=120)
                return "application/json", ct_call(["start", name], timeout=120)
            if op == "scale":
                return "application/json", ct_call(["scale", _ct_name(body.get("stack")), _ct_name(body.get("service")),
                                                    str(int(body.get("replicas")))], timeout=300)
            if op == "stack_rm":
                return "application/json", ct_call(["stack", "rm", _ct_name(body.get("stack"))], timeout=300)
            if op == "apply":
                if not isinstance(body.get("stack"), dict):
                    raise ValueError("apply needs the stack as a JSON object")
                os.makedirs(os.path.join(STORE, "web"), mode=0o700, exist_ok=True)  # screenshots, stacks (env values)
                f = os.path.join(STORE, "web", f"stack.{threading.get_ident()}.json")
                try:
                    with open(f, "w", encoding="utf-8") as out:
                        json.dump(body["stack"], out)
                    args = ["apply", f] + (["--project", check_project(body["project"])] if body.get("project") else [])
                    return "application/json", ct_call(args, timeout=300)
                finally:
                    with contextlib.suppress(OSError):
                        os.remove(f)
            raise ValueError(f"unknown op {op!r}")
        finally:
            _CT_CACHE.clear()  # a refresh during the action cached the old state
    raise NoRoute(path)


# ===== shared web dashboard: identical in terminal-ct and terminal-vm (their tests compare it; edit both) =====
# The daemon serves it on 127.0.0.1. GET / is the page (no data in it); every /api/ call needs the X-Token header,
# which a cross-site page cannot set without a CORS preflight this server never grants, and a Host header naming
# this server (DNS rebinding). Each tool defines around it: TOOL, WEB_PORT, STORE, WINDOWS, NO_WINDOW, in_wsl,
# check_project, daemon, daemon_alive, _daemon_file, and web_api(method, path, query, body) -> (content type,
# value): its own objects directly, the other tool's through that tool's JSON command line (cli_call).

class _Capture(threading.local):
    buf = None


_CAPTURE = _Capture()


class _Stdout:
    """sys.stdout once a thread captures: each thread's prints go to its own buffer, the others' to the real one
    (contextlib.redirect_stdout swaps it for every thread: the daemon's work and the web's actions mixed)."""
    def __init__(self, real):
        self.real = real

    def write(self, s):
        return (_CAPTURE.buf or self.real).write(s)

    def __getattr__(self, k):
        return getattr(_CAPTURE.buf or self.real, k)


@contextlib.contextmanager
def captured(buf):
    if not isinstance(sys.stdout, _Stdout):
        sys.stdout = _Stdout(sys.stdout)
    old, _CAPTURE.buf = _CAPTURE.buf, buf
    try:
        yield buf
    finally:
        _CAPTURE.buf = old


def peer_cli(env, exe):
    """The other tool's command: from the environment (e.g. TERMINAL_VM="python3 /path/terminal_vm.py"), else
    found on the PATH, else None."""
    if os.environ.get(env):
        return [a.strip('"') for a in shlex.split(os.environ[env], posix=not WINDOWS)]  # Windows paths keep their backslashes
    found = shutil.which(exe)
    return [found] if found else None


def cli_call(cli, args, timeout=60, lines=False, raw=False):
    """Run a JSON command line: its reply (the last line when progress lines come first; every line with lines;
    the text with raw). A failure raises RuntimeError with the tool's own message."""
    try:
        r = subprocess.run(cli + args, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                           errors="replace", **NO_WINDOW)
    except OSError as e:  # TERMINAL_VM naming a program that is not there
        raise RuntimeError(f"{cli[0]}: {e.strerror or e}") from None
    if r.returncode:
        err = (r.stderr or r.stdout).strip()
        with contextlib.suppress(ValueError, AttributeError, IndexError):
            err = json.loads(err.splitlines()[-1])["error"]
        raise RuntimeError(err or f"{os.path.basename(cli[-1])} {args[0]} failed")
    out = r.stdout if raw else r.stdout.strip()
    if raw:
        return out
    if lines:
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    try:
        return json.loads(out) if out else {}
    except ValueError:  # progress lines first: the reply is the last one
        return json.loads(out.splitlines()[-1])


class NoRoute(Exception):
    pass


def _web_write(path, text, mode=0o644):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    for i in range(20):  # Windows: refused while another process has the file open for a moment
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.05)


def web_token():
    """The dashboard's token (STORE/web.token, owner only): kept across daemon restarts, so open tabs go on."""
    path = os.path.join(STORE, "web.token")
    with contextlib.suppress(OSError):
        with open(path) as f:
            t = f.read().strip()
        if len(t) >= 32:
            return t
    # missing, or cut short (a crash while writing): an empty token would let any request in
    os.makedirs(STORE, exist_ok=True)
    t = secrets.token_urlsafe(24)
    _web_write(path, t, 0o600)
    return t


WEB_LAST = [0.0]  # time of the latest API call: a daemon with an open dashboard does not end for being idle
WEB_ROLES = ("read", "full")  # read: look (every GET); full: act too (POST)
WEB_SEEN = {}  # user: (time of its latest call, role): who is on the dashboard now


def _web_dir():
    d = os.path.join(STORE, "web")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def tls_cert(path, ip=None):
    """SHA-256 of the self-signed certificate kept at path (with its key, owner only), made once by openssl.
    ip: named in it too (subjectAltName), so a browser can trust it once, imported, instead of each time."""
    if not os.path.isfile(path):
        exe = shutil.which("openssl")
        if not exe:
            raise SystemExit("HTTPS needs openssl once, to make a certificate (Linux, macOS, WSL and Git for Windows "
                             "have it)")
        new, key = f"{path}.{os.getpid()}.new", f"{path}.{os.getpid()}.key"  # two first starts: each its own files
        argv = [exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", f"/CN={TOOL}",
                "-keyout", key, "-out", new]
        r = subprocess.run(argv + (["-addext", f"subjectAltName=IP:{ip}"] if ip else []), capture_output=True,
                           text=True, **NO_WINDOW)
        if r.returncode and ip:  # an openssl without -addext (old LibreSSL): a certificate without the address
            r = subprocess.run(argv, capture_output=True, text=True, **NO_WINDOW)
        try:
            if r.returncode:
                raise SystemExit(f"openssl: {r.stderr.strip()}")
            os.chmod(new, 0o600)
            with open(new, "a") as f, open(key) as k:  # one file, one replace: never a key of another start
                f.write(k.read())
            os.replace(new, path)
        finally:
            for x in (new, key):
                with contextlib.suppress(OSError):
                    os.remove(x)
    with open(path) as f:
        pem = f.read()
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem[:pem.index("-----END CERTIFICATE-----") + 25])).hexdigest()


def _web_users():
    try:
        with open(os.path.join(STORE, "web.users.json")) as f:
            u = json.load(f)
    except (OSError, ValueError):
        return {}
    return u if isinstance(u, dict) else {}


def web_user(cmd, name=None, role="full"):
    """Dashboard users, each with its own token (shown once: only its SHA-256 is kept) and role: read or full."""
    users, path = _web_users(), os.path.join(STORE, "web.users.json")
    if cmd == "ls":
        return [{"user": n, "role": u.get("role"), "added": u.get("added")} for n, u in sorted(users.items())]
    if cmd not in ("add", "rm"):
        raise SystemExit("web user add NAME [--role read|full] | web user ls | web user rm NAME")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,31}", name or "") or name == "owner":
        raise SystemExit(f"invalid user name {name!r} (lowercase letters, digits, _ . -; not owner)")
    if cmd == "rm":
        if name not in users:
            raise SystemExit(f"no dashboard user {name!r}")
        users.pop(name)
        _web_write(path, json.dumps(users, indent=1), 0o600)
        return {"removed": name}  # its open tabs are refused from their next call
    if role not in WEB_ROLES:
        raise SystemExit("--role read (look) or full (act too)")
    if name in users:
        raise SystemExit(f"user {name!r} exists: web user rm {name} first (a new token)")
    token = secrets.token_urlsafe(24)
    users[name] = {"sha256": hashlib.sha256(token.encode()).hexdigest(), "role": role,
                   "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
    os.makedirs(STORE, exist_ok=True)
    _web_write(path, json.dumps(users, indent=1), 0o600)
    out = {"user": name, "role": role, "token": token, "note": "shown once: give it to that person or agent"}
    net = _web_net()
    if net:
        out.update(url=f"{net['url']}/#t={token}", fingerprint=net["fingerprint"])
    return out


def _web_who(given):
    """(user, role) of a token, or None. The owner's token (web.token) is full."""
    if not given:
        return None
    if secrets.compare_digest(given, web_token().encode()):
        return "owner", "full"
    h = hashlib.sha256(given).hexdigest()
    for n, u in _web_users().items():
        if isinstance(u, dict) and u.get("role") in WEB_ROLES and secrets.compare_digest(h, str(u.get("sha256", ""))):
            return n, u["role"]
    return None


def _web_act_log(user, addr, path, body, error=None):
    """Who did what, from where (STORE/web/actions.log, one JSON line each, one older file kept)."""
    b = body if isinstance(body, dict) else {}
    clean = lambda v, n=64: re.sub(r"[\x00-\x1f\x7f]", "?", str(v))[:n]  # one line each, short: no forged lines
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": user, "from": addr, "path": clean(path),
           **{k: clean(b[k]) for k in ("op", "kind", "name", "service", "replicas") if isinstance(b.get(k), (str, int))}}
    st = b.get("stack")
    if isinstance(st, dict):
        st = st.get("stack")
    if isinstance(st, str):
        rec["stack"] = clean(st)
    if error:
        rec["error"] = clean(error, 200)
    log = os.path.join(_web_dir(), "actions.log")
    with contextlib.suppress(OSError):
        if os.path.exists(log) and os.path.getsize(log) > 2 ** 20:
            os.replace(log, log + ".1")
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def web_actions(n=20):
    """The latest actions done through the dashboard, newest first."""
    try:
        with open(os.path.join(STORE, "web", "actions.log"), encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def web_session(who):
    """This caller, who else is on the dashboard (a call in the last 30 s), and who did what."""
    now = time.time()
    return {"user": who[0], "role": who[1], "actions": web_actions(20),
            "online": [{"user": n, "role": r, "seen_s": int(now - t)}
                       for n, (t, r) in sorted(WEB_SEEN.items()) if now - t < 30]}


def _web_listen():
    """The network address the dashboard is shared on ({ip, port}; web --listen), or None."""
    try:
        with open(os.path.join(STORE, "web.listen.json")) as f:
            n = json.load(f)
        return {"ip": str(ipaddress.ip_address(n["ip"])), "port": int(n["port"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _web_net():
    """{url, fingerprint} of the network dashboard the running daemon serves, or None."""
    try:
        with open(_daemon_file("webnet")) as f:
            n = json.load(f)
        return n if _web_listen() and n.get("url") and n.get("fingerprint") else None
    except (OSError, ValueError, AttributeError):
        return None


class _WebServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not WINDOWS  # on Windows it lets a second process bind the same port
    allow_reuse_port = False  # never share the port with another store's daemon
    slots = None  # at most 64 clients at once: idle connections never use up threads or memory

    def handle_error(self, *a):  # a failed handshake, a client gone: just that client
        pass

    per_address = None  # the network side: at most this many at once from one address (one host cannot take all)

    def process_request(self, request, client_address):
        if self.slots is None:  # (the agent's server sets its own)
            self.slots = threading.BoundedSemaphore(64)
        if getattr(self, "lock", None) is None:
            self.by_address, self.lock, self.origin = {}, threading.Lock(), {}  # origin: request id -> address
        with self.lock:
            busy = self.per_address and self.by_address.get(client_address[0], 0) >= self.per_address
            if not busy and self.slots.acquire(blocking=False):
                self.by_address[client_address[0]] = self.by_address.get(client_address[0], 0) + 1
                self.origin[id(request)] = client_address[0]  # (a plain socket takes no attribute)
            else:
                return socketserver.TCPServer.shutdown_request(self, request)  # full: closed, no slot to give back
        super().process_request(request, client_address)  # its thread failing to start: shutdown_request below

    def shutdown_request(self, request):  # end of a request that had a slot
        try:
            super().shutdown_request(request)
        finally:
            with self.lock:
                a = self.origin.pop(id(request), None)
                if self.by_address.get(a, 0) > 1:
                    self.by_address[a] -= 1
                else:
                    self.by_address.pop(a, None)
            self.slots.release()

    def server_bind(self):  # HTTPServer's also looks up this host's FQDN: seconds on some Macs, and unused here
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _WebHandler(http.server.BaseHTTPRequestHandler):
    server_version = "terminal-dashboard"
    timeout = 30  # a client stalling mid-request frees its thread

    def log_message(self, *a):  # quiet: the daemon's log is for its own work
        pass

    def version_string(self):  # no Python version told to whoever asks
        return self.server_version

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):  # the network's HTTPS: its handshake here, in this request's thread
            self.request.settimeout(self.timeout)
            self.request.do_handshake()
        super().setup()

    def _send(self, code, ctype, body):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                     ("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'unsafe-inline'; "
                                                 "script-src 'unsafe-inline'; frame-ancestors 'none'")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        if self.headers.get("Host") not in self.server.hosts:
            return self._send(403, "application/json", {"error": "unknown host"})
        u = urllib.parse.urlsplit(self.path)
        if method == "GET" and u.path == "/":
            return self._send(200, "text/html; charset=utf-8", WEB_PAGE.encode())
        given = self.headers.get("X-Token", "").encode("utf-8", "replace")
        who = _web_who(given)
        if not who:
            return self._send(401, "application/json", {"error": f"token: open the dashboard with {TOOL} web, or ask "
                                                                 f"for one ({TOOL} web user add NAME)"})
        WEB_LAST[0] = time.time()
        WEB_SEEN[who[0]] = (WEB_LAST[0], who[1])
        addr, body = self.client_address[0], None
        try:
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 <= n <= 2 ** 20:
                    raise ValueError("body too large")
                raw = self.rfile.read(n)  # read even when refused: unread data makes Windows reset the connection
                if self.headers.get_content_type() != "application/json":  # no cross-site "simple" request
                    raise ValueError("Content-Type must be application/json")
                body = json.loads(raw or b"null")
                if who[1] != "full":
                    _web_act_log(who[0], addr, u.path, body, "refused: read only")
                    return self._send(403, "application/json", {"error": "read only: this token may look, not act"})
            if method == "GET" and u.path == "/api/session":
                return self._send(200, "application/json", web_session(who))
            ctype, out = web_api(method, u.path, dict(urllib.parse.parse_qsl(u.query)), body)
            if method == "POST":
                _web_act_log(who[0], addr, u.path, body)
            self._send(200, ctype, out)
        except NoRoute:
            self._send(404, "application/json", {"error": "not found"})
        except ConnectionError:  # the browser left: no error reply on a closed socket
            raise
        except (SystemExit, Exception) as e:  # a bad request or a failed action: the server goes on
            if method == "POST":
                _web_act_log(who[0], addr, u.path, body, str(e) or type(e).__name__)
            self._send(400, "application/json", {"error": str(e) or type(e).__name__})

    def handle(self):
        with contextlib.suppress(ConnectionError):  # the browser left before the reply (closed or frozen tab)
            super().handle()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def web_serve(port=None):
    """Serve the dashboard until the process ends (a thread of the daemon); its port goes to STORE/daemon.web."""
    try:
        web_token()  # made now: the web command reads it once the port is up
        try:
            srv = _WebServer(("127.0.0.1", WEB_PORT if port is None else port), _WebHandler)
        except OSError:  # taken: any free port (the web command reads it from the file)
            srv = _WebServer(("127.0.0.1", 0), _WebHandler)
        p = srv.server_address[1]
        srv.hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
        _web_write(_daemon_file("web"), json.dumps({"port": p, "pid": os.getpid()}))
        with contextlib.suppress(OSError):
            os.remove(_daemon_file("webnet"))  # an earlier daemon's: said again below if this one shares it
        if _web_listen():
            threading.Thread(target=_web_serve_net, args=(_web_listen(),), daemon=True).start()
        srv.serve_forever()
    except Exception as e:  # the dashboard down must not take the daemon's own work with it
        print(f"{time.strftime('%H:%M:%S')} web: {e}", file=sys.stderr, flush=True)


class _WebServer6(_WebServer):
    address_family = socket.AF_INET6


def _web_serve_net(net):
    """The dashboard on the network too (web --listen): HTTPS only, the same tokens, at most 64 clients at once."""
    try:
        cert = os.path.join(_web_dir(), f"cert-{net['ip'].replace(':', '_')}.pem")  # one per address: named in it
        fp = tls_cert(cert, net["ip"])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert)
        ip = ipaddress.ip_address(net["ip"])
        srv = (_WebServer6 if ip.version == 6 else _WebServer)((str(ip), net["port"]), _WebHandler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
        name = f"[{ip}]" if ip.version == 6 else str(ip)
        host = f"{name}:{srv.server_address[1]}"
        srv.hosts = {host} | ({name} if srv.server_address[1] == 443 else set())  # a browser names this address
        srv.per_address = 8
        _web_write(_daemon_file("webnet"), json.dumps({"url": f"https://{host}", "fingerprint": fp, "pid": os.getpid()}))
        srv.serve_forever()
    except Exception as e:  # the local dashboard and the daemon's work go on
        print(f"{time.strftime('%H:%M:%S')} web (network): {e}", file=sys.stderr, flush=True)


def web_cli(args, role="full", open_browser=True, project=None, listen=None, port=None):
    """The web command of both tools: web [--listen IP|off] [--port N], or web user add|ls|rm."""
    if not args:
        return web(open_browser, project, listen, port)
    if args[0] != "user" or len(args) > 3:
        raise SystemExit("web [--listen IP|off] [--port N] | web user add NAME [--role read|full] | web user ls | "
                         "web user rm NAME")
    return web_user(*args[1:3], role=role)


def web(open_browser=True, project=None, listen=None, port=None):
    """Start the daemon if needed, then open its dashboard in the browser; returns the URL (token included).
    listen: share it on the network too, at that address of this PC (HTTPS), or "off"."""
    if project:
        check_project(project)
    if listen is not None:
        path = os.path.join(STORE, "web.listen.json")
        if listen == "off":
            with contextlib.suppress(OSError):
                os.remove(path)
        else:
            try:
                ip = ipaddress.ip_address(listen)
            except ValueError:
                raise SystemExit("--listen: an address of this PC on the network (an IP), or off") from None
            if ip.is_unspecified or ip.version == 6 and (ip.ipv4_mapped and ip.ipv4_mapped.is_unspecified
                                                         or "%" in str(ip)):
                raise SystemExit("--listen: one address of this PC (not 0.0.0.0 or ::, which would share it on every "
                                 "network; no %zone)")
            p = WEB_PORT + 10 if port is None else port
            if not 1 <= p <= 65535:
                raise SystemExit("--port: 1-65535")
            tls_cert(os.path.join(_web_dir(), f"cert-{str(ip).replace(':', '_')}.pem"), str(ip))  # no openssl: said now
            os.makedirs(STORE, exist_ok=True)
            _web_write(path, json.dumps({"ip": str(ip), "port": p}))
        if daemon_alive():  # it serves what it read when it started
            daemon("stop")
            end = time.time() + 60
            while daemon_alive() and time.time() < end:
                time.sleep(0.3)
            if daemon_alive():  # it would go on serving what it read before
                raise SystemExit(f"the daemon did not stop: the dashboard may still be shared as before; "
                                 f"{TOOL} daemon stop, then {TOOL} web again")
    if not daemon_alive():
        daemon("start")
    end = time.time() + 20
    while True:
        try:
            with open(_daemon_file("web")) as f:
                w = json.load(f)
            with open(_daemon_file("beat")) as f:
                if int(f.read().split()[1]) != w["pid"]:  # left by an earlier daemon
                    raise ValueError("stale")
            socket.create_connection(("127.0.0.1", w["port"]), 1).close()
            break
        except (OSError, ValueError, KeyError, IndexError):
            if time.time() > end:
                raise SystemExit(f"the daemon serves no dashboard (started by an older version?): "
                                 f"{TOOL} daemon stop, then {TOOL} web") from None
            time.sleep(0.3)
    url = f"http://127.0.0.1:{w['port']}/#t={web_token()}" + (f"&p={urllib.parse.quote(project)}" if project else "")
    net = None
    if _web_listen():  # its network side comes up a moment after
        for _ in range(30):
            net = _web_net()
            if net:
                break
            time.sleep(0.3)
    if open_browser:
        with contextlib.suppress(OSError):
            if in_wsl():  # the Windows browser: WSL forwards its 127.0.0.1 ports to Windows
                subprocess.Popen(["rundll32.exe", "url.dll,FileProtocolHandler", url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(url)
    out = {"url": url, "port": w["port"]}
    if net:
        out["network"] = {"url": net["url"], "fingerprint": net["fingerprint"],
                          "users": f"{TOOL} web user add NAME --role read|full: a token for each person or agent"}
    elif _web_listen():
        out["network"] = {"error": f"not served: see {_daemon_file('log')}"}
    return out


WEB_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal dashboard</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--fg:#1d2330;--mute:#6b7385;--line:#e2e5ea;--acc:#2f6fdb;--ok:#1f9d55;--warn:#c98a00;--bad:#d64545;--cpu:#2f6fdb;--mem:#8a4fd8}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--panel:#1a1f27;--fg:#e4e8ef;--mute:#8e97a8;--line:#2a313c;--acc:#6aa0ff;--ok:#3cc47c;--warn:#e0b040;--bad:#ff6b6b;--cpu:#6aa0ff;--mem:#b68cff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
header h1{font-size:16px;margin:0}.chip{padding:2px 8px;border:1px solid var(--line);border-radius:10px;color:var(--mute);font-size:12px}
main{padding:12px 16px;display:grid;gap:14px}section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow-x:auto}
h2{font-size:14px;margin:0 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--mute);font-weight:500;font-size:12px}
.running,.ok{color:var(--ok)}.created,.paused,.failing{color:var(--warn)}.exited,.unhealthy,.dead{color:var(--bad)}
button{font:inherit;font-size:12px;padding:2px 8px;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--fg);cursor:pointer;margin-right:3px}
button:hover{border-color:var(--acc)}button.danger:hover{border-color:var(--bad);color:var(--bad)}button:disabled{opacity:.5;cursor:wait}
.shots{display:flex;flex-wrap:wrap;gap:10px}.shot{width:240px;cursor:pointer}.shot img{width:100%;aspect-ratio:16/10;object-fit:contain;background:#000;border-radius:5px;display:block}
.shot div{font-size:12px;color:var(--mute);margin-top:3px}canvas{vertical-align:middle;margin-right:4px}
#msg{color:var(--mute);font-size:12px;min-height:1em}#msg.err{color:var(--bad)}
textarea{width:100%;min-height:180px;font:12px ui-monospace,monospace;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:6px}
.ev{font:12px ui-monospace,monospace;white-space:pre-wrap;max-height:220px;overflow:auto;margin:0}
dialog{width:min(1100px,96vw);max-height:92vh;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--fg);padding:10px}
dialog img{max-width:100%;max-height:78vh;display:block;margin:auto}dialog pre{max-height:76vh;overflow:auto;font:12px ui-monospace,monospace;white-space:pre-wrap}
select{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:5px}.mute{color:var(--mute)}
</style></head><body>
<header><h1 id="tool">terminal</h1><span id="engine" class="chip">…</span><span id="daemon" class="chip">daemon …</span><span id="who" class="chip"></span><span id="online" class="mute"></span>
<label class="mute">project <select id="project"><option value="">all</option></select></label><span id="quota" class="mute"></span>
<span id="msg"></span></header>
<main>
<section><h2>Screens</h2><div id="shots" class="shots"><span class="mute">no running desktop</span></div></section>
<section><h2>Containers</h2><table><thead><tr><th>name</th><th>status</th><th>health</th><th>project</th><th>stack</th><th>CPU</th><th>memory</th><th>age</th><th>restarts</th><th>flags</th><th></th></tr></thead><tbody id="cts"></tbody></table><div id="ctnote" class="mute"></div></section>
<section><h2>VMs (terminal-vm)</h2><table><thead><tr><th>name</th><th>os</th><th>status</th><th>project</th><th>CPU</th><th>memory</th><th></th></tr></thead><tbody id="vms"></tbody></table><div id="vmnote" class="mute"></div></section>
<section><h2>Stacks</h2><table><thead><tr><th>stack</th><th>project</th><th>services (running / wanted)</th><th></th></tr></thead><tbody id="stacks"></tbody></table>
<p><button id="new">new stack</button></p><div id="editor" hidden><textarea id="spec" spellcheck="false"></textarea><p><button id="apply">apply</button><button id="close">close</button></p></div></section>
<section><h2>Events</h2><pre id="events" class="ev"></pre></section>
<section><h2>Actions (who did what)</h2><pre id="actions" class="ev"></pre></section>
</main>
<dialog id="dlg"><p><b id="dlgtitle"></b> <button id="dlgclose" style="float:right">close</button></p><div id="dlgbody"></div></dialog>
<script>
"use strict";
const $ = id => document.getElementById(id);
const h = (tag, props, ...kids) => { const e = document.createElement(tag); Object.assign(e, props || {});
  for (const k of kids) if (k != null) e.append(k instanceof Node ? k : String(k)); return e; };
let token = sessionStorage.getItem("tct-token") || "";
const frag = new URLSearchParams(location.hash.slice(1));
if (frag.get("t")) { token = frag.get("t"); sessionStorage.setItem("tct-token", token); }
if (frag.get("p")) sessionStorage.setItem("tct-project", frag.get("p"));
history.replaceState(null, "", location.pathname);  // the token out of the address bar
const hist = {}, HIST = 60; let last = null, shotTimer = 0, quotaBusy = false;
const project = () => $("project").value;
function msg(t, err) { $("msg").textContent = t; $("msg").className = err ? "err" : ""; }
async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"X-Token": token, "Content-Type": "application/json"}}, opts || {}));
  if (!r.ok) { let e = r.status + ""; try { e = (await r.json()).error; } catch (_) {} throw new Error(e); }
  return r;
}
const q = o => new URLSearchParams(Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v != null)));
async function act(body, button, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  if (button) button.disabled = true;
  const what = (body.op || "") + " " + (body.name || (body.stack && typeof body.stack === "object" ? body.stack.stack : body.stack) || "");
  msg(what + " …");
  try { const r = await (await api("/api/do", {method: "POST", body: JSON.stringify(body)})).json();
    msg(what + ": done" + (r && r.desktop_ready != null ? " (desktop ready in " + r.desktop_ready + " s)" : "")); return r; }
  catch (e) { msg(e.message, true); } finally { if (button) button.disabled = false; refresh(); }
}
function spark(values, max, colour) {
  const c = h("canvas", {width: 70, height: 18}), x = c.getContext("2d");
  x.strokeStyle = colour; x.lineWidth = 1.5; x.beginPath();
  values.forEach((v, i) => { const px = i * 70 / (HIST - 1), py = 17 - Math.min(1, (v || 0) / (max || 1)) * 16;
    i ? x.lineTo(px, py) : x.moveTo(px, py); });
  x.stroke(); return c;
}
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const age = s => s == null ? "-" : s < 120 ? Math.round(s) + "s" : s < 7200 ? Math.round(s / 60) + "m" : s < 172800 ? Math.round(s / 3600) + "h" : Math.round(s / 86400) + "d";
function button(label, body, confirmText, cls) {
  const b = h("button", {textContent: label, className: cls || ""}); b.onclick = () => act(body, b, confirmText); return b;
}
// the cells of buttons survive the refresh while their state is the same: rebuilt every 2 s, the button under the
// pointer was replaced, and a click landing on the swap was lost
function kept(tb) { const m = new Map(); for (const tr of tb.children) if (tr.cells_) m.set(tr.dataset.key, tr.cells_); return m; }
function cell(prev, key, sig, make) {
  const old = (prev.get(key) || {})[sig.split("|")[0]];
  if (old && old.sig === sig) return old;
  const td = h("td", null, ...make()); td.sig = sig; return td;
}
function keyed(key, cells, ...tds) { const tr = h("tr", null, ...tds); tr.dataset.key = key; tr.cells_ = cells; return tr; }
function renderCts(d) {
  const tb = $("cts"), prev = kept(tb); tb.replaceChildren();
  for (const r of d.rows) {
    const hh = hist[r.name] = hist[r.name] || {cpu: [], mem: []};
    hh.cpu.push(r.cpu_percent); hh.mem.push(r.used_memory_gb);
    const memMax = typeof r.memory_gb === "number" ? r.memory_gb : Math.max(1, ...hh.mem.filter(v => v != null));
    const cpuMax = 100 * (typeof r.cpus === "number" ? r.cpus : 1);
    if (hh.cpu.length > HIST) { hh.cpu.shift(); hh.mem.shift(); }
    const run = r.status === "running";
    const acts = cell(prev, r.name, "acts|" + r.status + "|" + r.flags + "|" + r.stack, () => [
        run ? button("stop", {op: "stop", name: r.name}) : button("start", {op: "start", name: r.name}),
        button("restart", {op: "restart", name: r.name}), logsButton(r.name),
        r.flags.includes("D") && run ? shotButton("ct", r.name) : null,
        button("rm", {op: "rm", name: r.name}, "Remove " + r.name + " and its /data?" + (r.stack ? " (a stack replica: the daemon makes it again, with an empty /data)" : ""), "danger")]);
    tb.append(keyed(r.name, {acts}, h("td", null, h("b", {textContent: r.name}),
      r.node ? h("span", {className: "mute", textContent: " @" + r.node, title: "in node " + r.node}) : null,
      r.description ? h("div", {className: "mute", textContent: r.description}) : null), h("td", {className: r.status, textContent: r.status}),
      h("td", {className: r.health.split(" ")[0], textContent: r.health}), h("td", {textContent: r.project}), h("td", {textContent: r.stack || "-"}),
      h("td", null, spark(hh.cpu, Math.max(100, cpuMax), css("--cpu")), r.cpu_percent == null ? "-" : r.cpu_percent + "%"),
      h("td", null, spark(hh.mem, memMax, css("--mem")), (r.used_memory_gb == null ? "-" : r.used_memory_gb) + " / " + (r.memory_gb || "-") + " GiB"),
      h("td", {textContent: age(r.age)}), h("td", {textContent: r.restarts}), h("td", {textContent: r.flags, title: "D desktop, S sandbox, I internet, A agent's"}),
      acts));
  }
  for (const k of Object.keys(hist)) if (!d.rows.some(r => r.name === k)) delete hist[k];
  if (!d.rows.length) tb.append(h("tr", null, h("td", {colSpan: 11, className: "mute", textContent: "no container"})));
}
function logsButton(name) {
  const b = h("button", {textContent: "logs"});
  b.onclick = async () => { const pre = h("pre"); openDlg("logs " + name, pre);
    try { pre.textContent = await (await api("/api/logs?" + q({name, n: 300}))).text(); pre.scrollTop = 1e9; } catch (e) { pre.textContent = e.message; } };
  return b;
}
function shotButton(kind, name) { const b = h("button", {textContent: "screen"}); b.onclick = () => bigShot(kind, name); return b; }
let dlgTimer = 0, dlgGen = 0;  // dlgGen: a refresh loop of an earlier view stops by itself
function openDlg(title, body) { dlgGen++; clearTimeout(dlgTimer); $("dlgtitle").textContent = title; $("dlgbody").replaceChildren(body); $("dlg").showModal(); }
$("dlgclose").onclick = () => $("dlg").close();
$("dlg").onclose = () => { dlgGen++; clearTimeout(dlgTimer);
  $("dlgbody").querySelectorAll("img").forEach(i => { if (i.src.startsWith("blob:")) URL.revokeObjectURL(i.src); }); };
async function loadShot(img, kind, name) {
  if (img.busy) return; img.busy = true;  // a slow capture (a VM: seconds): never two at once for one image
  try { const r = await api("/api/shot?" + q({kind, name})), url = URL.createObjectURL(await r.blob());
    const old = img.src; img.src = url; if (old.startsWith("blob:")) URL.revokeObjectURL(old); }
  finally { img.busy = false; }
}
function bigShot(kind, name) {
  const img = h("img", {alt: name}); openDlg((kind === "vm" ? "VM " : "") + name, img);
  const gen = dlgGen;
  const tick = async () => { try { if (!document.hidden) await loadShot(img, kind, name); } catch (e) { msg(e.message, true); }
    if ($("dlg").open && gen === dlgGen) dlgTimer = setTimeout(tick, kind === "vm" ? 2000 : 1000); };
  tick();
}
const shotEls = {};
async function renderShots(d, vms) {
  const want = d.rows.filter(r => r.flags.includes("D") && r.status === "running").map(r => ["ct", r.name])
    .concat(vms.filter(v => v.status === "running").map(v => ["vm", v.name]));  // looking wakes nothing
  const box = $("shots"), keys = want.map(w => w.join(":"));
  for (const k of Object.keys(shotEls)) if (!keys.includes(k)) { const s = shotEls[k].img.src;
    if (s.startsWith("blob:")) URL.revokeObjectURL(s); shotEls[k].remove(); delete shotEls[k]; }
  for (const [kind, name] of want) {
    const k = kind + ":" + name;
    if (!shotEls[k]) { const img = h("img", {alt: name}); shotEls[k] = h("div", {className: "shot", onclick: () => bigShot(kind, name)}, img,
        h("div", {textContent: (kind === "vm" ? "VM " : "") + name})); shotEls[k].img = img; box.append(shotEls[k]); }
  }
  if (!want.length) box.replaceChildren(h("span", {className: "mute", textContent: "no running desktop"}));
  else box.querySelectorAll(":scope > span").forEach(e => e.remove());
  if (Date.now() - shotTimer > 5000 && !document.hidden) { shotTimer = Date.now();
    for (const [kind, name] of want) loadShot(shotEls[kind + ":" + name].img, kind, name).catch(() => {}); }
}
function renderVms(v) {
  const tb = $("vms"), prev = kept(tb); tb.replaceChildren(); $("vmnote").textContent = v.note || "";
  for (const m of v.vms) {
    const run = m.status && m.status !== "stopped";
    const acts = cell(prev, m.name, "acts|" + run + "|" + (m.engine || ""), () => [
      m.engine ? null : run ? button("stop", {kind: "vm", op: "stop", name: m.name}, "Stop VM " + m.name + "?")
        : button("start", {kind: "vm", op: "start", name: m.name}), run ? shotButton("vm", m.name) : null,
      run && !m.engine ? button("force stop", {kind: "vm", op: "force_stop", name: m.name}, "Power off VM " + m.name + " at once (like pulling the plug)?", "danger") : null]);
    tb.append(keyed(m.name, {acts}, h("td", null, h("b", {textContent: m.name}),
      m.description ? h("div", {className: "mute", textContent: m.description}) : null), h("td", {textContent: m.os || "-"}),
      h("td", {className: run ? "running" : "exited", textContent: m.status || "-"}), h("td", {textContent: m.project || "-"}),
      h("td", {textContent: m.cpu_percent == null ? "-" : m.cpu_percent + "%"}),
      h("td", {textContent: (m.host_memory_gb == null ? "-" : m.host_memory_gb) + " / " + (m.memory_gb || "-") + " GiB"}),
      acts));
  }
  if (!v.vms.length) tb.append(h("tr", null, h("td", {colSpan: 7, className: "mute", textContent: "no VM"})));
}
function renderStacks(d) {
  const tb = $("stacks"), prev = kept(tb); tb.replaceChildren();
  for (const s of d.stacks) {
    const counts = Object.entries(s.services).map(([svc, c]) => [svc, c.replicas,
      d.rows.filter(r => r.stack.startsWith(s.stack + "/" + svc + "/") && r.status === "running").length]);
    const svcs = cell(prev, s.stack, "svcs|" + JSON.stringify(counts), () => counts.flatMap(([svc, want, runningN]) => {
      const sc = n => button(n > want ? "+" : "−", {op: "scale", stack: s.stack, service: svc, replicas: n});
      return [h("span", {textContent: svc + " " + runningN + "/" + want + " "}), want > 0 ? sc(want - 1) : null, sc(want + 1), " "];
    }));
    const acts = cell(prev, s.stack, "acts|", () => { const edit = h("button", {textContent: "edit"}); edit.onclick = () => editStack(s.stack);
      return [edit, button("rm", {op: "stack_rm", stack: s.stack}, "Remove stack " + s.stack + ", its containers and their /data?", "danger")]; });
    tb.append(keyed(s.stack, {svcs, acts}, h("td", null, h("b", {textContent: s.stack})), h("td", {textContent: s.project}), svcs, acts));
  }
  if (!d.stacks.length) tb.append(h("tr", null, h("td", {colSpan: 4, className: "mute", textContent: "no stack"})));
}
async function editStack(name) {
  try { const r = await (await api("/api/stack?" + q({name}))).json(); $("spec").value = JSON.stringify(r.desired, null, 1); $("editor").hidden = false; }
  catch (e) { msg(e.message, true); }
}
$("new").onclick = () => { $("spec").value = JSON.stringify({stack: "web", project: project() || undefined,
  services: {app: {image: "nginx", replicas: 2, cpus: 0.5, memory_gb: 0.5, restart: "always"}}}, null, 1); $("editor").hidden = false; };
$("close").onclick = () => { $("editor").hidden = true; };
$("apply").onclick = async () => { let s; try { s = JSON.parse($("spec").value); } catch (e) { return msg("JSON: " + e.message, true); }
  if (await act({op: "apply", stack: s}, $("apply"))) $("editor").hidden = true; };
function renderTop(d, v) {
  if (d.tool) {  // a state reply (the quota refresh passes only the quota)
  $("tool").textContent = d.tool; $("ctnote").textContent = d.note || "";
  $("engine").textContent = d.engine || "no container engine"; $("daemon").textContent = "daemon " + (d.daemon ? "running" : "stopped");
  $("daemon").style.color = d.daemon ? css("--ok") : css("--bad");
  const evs = (d.events || []).concat((v && v.events) || []).sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0)).slice(0, 12);
  $("events").textContent = evs.map(e => [e.time, e.event, e.stack, e.service, e.container, e.vm && "VM " + e.vm, e.detail].filter(Boolean).join("  ")).join("\n") || "no event";
  }
  if (d.quota) last = d.quota;
  if (last) $("quota").textContent = Object.entries(last).map(([k, v]) => k.replace("_gb", "") + " " + (v.used ?? "?") + "/" + (v.limit ?? "∞")).join(" · ");
  const sel = $("project"), cur = sel.value, names = [...new Set((d.projects || []).concat(cur ? [cur] : []))].sort();
  if (d.projects && names.join("\n") !== [...sel.options].slice(1).map(o => o.value).join("\n")) {  // rebuilt only when it changed: an open list stays open
    sel.replaceChildren(h("option", {value: "", textContent: "all"}), ...names.map(p => h("option", {value: p, textContent: p})));
    sel.value = cur; }
}
let busy = false, again = false;
function renderSession(s) {
  if (!s) return;
  $("who").textContent = s.user + (s.role === "read" ? " (read only)" : "");
  const others = s.online.filter(o => o.user !== s.user).map(o => o.user + (o.role === "read" ? " (read)" : ""));
  $("online").textContent = others.length ? "with " + others.join(", ") : "";
  $("actions").textContent = s.actions.map(a => [a.time, a.user, a.from, a.op || a.path, a.kind, a.stack, a.service,
    a.name, a.replicas, a.error && "failed: " + a.error].filter(x => x !== undefined && x !== "").join("  ")).join("\n")
    || "no action yet";
}
async function refresh() {
  if (busy) { again = true; return; }  // asked meanwhile (an action, another project): once this pass ends
  if (document.hidden) return; busy = true;  // a forgotten tab costs the engine nothing
  try {
    const p = project();
    const [d, v] = await Promise.all([api("/api/state?" + q({project: p})).then(r => r.json()),
      api("/api/vms?" + q({project: p})).then(r => r.json()).catch(e => ({vms: [], note: e.message}))]);
    if (p !== project()) return;  // the project changed meanwhile: the next pass shows it
    renderTop(d, v); renderCts(d); renderVms(v); renderStacks(d); renderShots(d, v.vms);
    renderSession(await api("/api/session").then(r => r.json()).catch(() => null));
    if ($("msg").className === "err" && $("msg").textContent.startsWith("dashboard:")) msg("");
  } catch (e) { msg("dashboard: " + e.message, true); }
  finally { busy = false; if (again) { again = false; refresh(); } }
}
async function refreshQuota() {  // apart: measuring the disk takes seconds, the table must not wait for it
  if (quotaBusy || document.hidden) return; quotaBusy = true;
  try { const p = project(), r = await (await api("/api/quota?" + q({project: p}))).json();
    if (p === project()) renderTop({quota: r.quota, projects: r.projects}); }
  catch (e) { msg("quota: " + e.message, true); } finally { quotaBusy = false; }
}
$("project").onchange = () => { sessionStorage.setItem("tct-project", project()); last = null; $("quota").textContent = ""; refresh(); refreshQuota(); };
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); refreshQuota(); } });
const saved = sessionStorage.getItem("tct-project");
if (saved) $("project").append(h("option", {value: saved, textContent: saved})), $("project").value = saved;
if (!token) msg("no token: open the dashboard with the web command (terminal-ct web, terminal-vm web)", true); else { refresh(); refreshQuota(); setInterval(refresh, 2000); setInterval(refreshQuota, 15000); }
</script></body></html>
"""
# ===== end of shared web dashboard =====


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


def net_create(net, project=None):
    path = net_file(net)
    project = check_project(project or DEFAULT_PROJECT)
    with quota_lock():  # the host-wide lock also makes the index allocation atomic
        if os.path.exists(path):
            raise SystemExit(f"network {net!r} already exists")
        used = {net_load(n)["index"] for n in networks()}
        idx = next((i for i in range(1, 251) if i not in used), None)
        if idx is None:
            raise SystemExit("250 networks at most")
        cfg = {"name": net, "index": idx, "port": 40000 + idx, "subnet": f"10.77.{idx}.0/24", "project": project}
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
    set_description("net", net, "")
    return {"removed": net}


def net_ls():
    return [dict(net_load(n), vms=[{"vm": v, **a} for v, a in net_members(n)],
                 **({"description": t} if (t := description("net", n)) else {})) for n in networks()]


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
                           capture_output=True, text=True, **NO_WINDOW)
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
    print(json.dumps(vm_list() + mac_machines(), indent=1))


def vm_list():
    out = []
    if os.path.isdir(STORE):
        for n in sorted(os.listdir(STORE)):
            if os.path.isfile(os.path.join(STORE, n, "machine.json")):
                m = load(n)
                out.append({"name": n, "os": m.get("os", "linux"), "status": state(n), "project": project_of(m),
                            "cpus": m["cpus"], "memory_gb": m["memory_mb"] // 1024,
                            "display": f"{m.get('width', 1280)}x{m.get('height', 800)}", "layout": m.get("layout", "us"),
                            **({"base": m["base"]} if m.get("base") else {}), **({"sandbox": True} if m.get("sandbox") else {}),
                            **({"description": t} if (t := description("vm", n)) else {})})
    return out


def resume_all(stay=False):
    """Start every VM that was running when the machine stopped (within the quota). stay: then keep this process
    alive while any VM runs (WSL shuts its VM down once no Windows process uses it)."""
    out = []
    for n in machines():
        if wanted(n) == "running" and state(n) == "stopped":
            try:
                out.append(dict(quiet(start, n), name=n))
            except (SystemExit, Exception) as e:  # one refusal (quota) must not keep the others down
                out.append({"name": n, "error": str(e)})
    print(json.dumps({"resumed": out}))
    while stay and any(state(n) != "stopped" for n in machines()):
        time.sleep(60)


AUTOSTART = "terminal-vm-resume"


def autostart(on=None):
    """Run `resume` at each logon: a Windows scheduled task (for WSL: wsl.exe running it inside the distribution,
    kept alive while VMs run), else a systemd user service on Linux."""
    me = [sys.executable, os.path.abspath(__file__)]
    unit = os.path.expanduser(f"~/.config/systemd/user/{AUTOSTART}.service")
    env = {k: os.environ[k] for k in ("TERMINAL_VM_HOME", "TERMINAL_LINUX_HOME", "XDG_DATA_HOME") if os.environ.get(k)}
    if on:  # VMs running now, started before `want` was kept: they are to come back too
        for n in machines():
            if state(n) != "stopped" and not os.path.isfile(os.path.join(vm_dir(n), "want")):
                want(n, "running")
    if WINDOWS or in_wsl():
        schtasks = "schtasks.exe" if in_wsl() else "schtasks"
        if on is None:
            return {"autostart": subprocess.run([schtasks, "/Query", "/TN", AUTOSTART], capture_output=True).returncode == 0}
        if not on:
            subprocess.run([schtasks, "/Delete", "/TN", AUTOSTART, "/F"], capture_output=True)
            return {"autostart": False}
        if in_wsl():  # conhost --headless: no console window at logon; wsl.exe reads a Windows command line
            cmd = "conhost.exe --headless " + subprocess.list2cmdline(
                ["wsl.exe", "-d", os.environ.get("WSL_DISTRO_NAME", "Debian"), "--exec", "env",
                 *(f"{k}={v}" for k, v in env.items()), *me, "resume", "--stay"])
        else:
            if env:  # shortcut: a task's command line sets no variables; set them as user variables to use this
                raise SystemExit(f"autostart on Windows uses the default store; unset {', '.join(env)} first")
            cmd = subprocess.list2cmdline(me + ["resume"])
        if len(cmd) > 261:  # schtasks /TR limit
            raise SystemExit(f"the task's command line is {len(cmd)} characters, schtasks takes 261: {cmd}")
        r = subprocess.run([schtasks, "/Create", "/TN", AUTOSTART, "/SC", "ONLOGON", "/TR", cmd, "/F"],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit((r.stderr or r.stdout).strip())
        return {"autostart": True, "task": AUTOSTART, "runs": cmd}
    if on is None:
        return {"autostart": os.path.isfile(unit)}
    if not on:
        subprocess.run(["systemctl", "--user", "disable", f"{AUTOSTART}.service"], capture_output=True)
        with contextlib.suppress(OSError):
            os.remove(unit)
        return {"autostart": False}
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    with open(unit, "w") as f:  # no After=default.target: with WantedBy it made an ordering cycle
        # RemainAfterExit + KillMode=process: resume exits at once, and systemd would otherwise kill the QEMU
        # processes it started (same cgroup) as the unit ends
        f.write("[Unit]\nDescription=terminal-vm: start again the VMs that were running\n\n[Service]\nType=oneshot\n"
                "RemainAfterExit=yes\nKillMode=process\n"
                + "".join(f"Environment={shlex.quote(f'{k}={v}')}\n" for k, v in env.items())
                + f"ExecStart={shlex.join(me + ['resume'])}\n\n[Install]\nWantedBy=default.target\n")
    r = subprocess.run(["systemctl", "--user", "enable", f"{AUTOSTART}.service"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit((r.stderr or r.stdout).strip())
    return {"autostart": True, "unit": unit, "note": "runs at your login; for boot without login: loginctl enable-linger"}


def projects():
    names = {DEFAULT_PROJECT} | {project_of(load(n)) for n in machines()} | {net_load(n).get("project", "default")
                                                                            for n in networks()}
    if os.path.isdir(os.path.join(STORE, ".projects")):
        names |= {d.replace("+", "/") for d in os.listdir(os.path.join(STORE, ".projects"))}
    vms = [(n, project_of(load(n))) for n in machines()]
    return [{"project": p, "vms": sum(q == p for _, q in vms), "running": sum(q == p and state(n) != "stopped" for n, q in vms),
             "networks": sum(net_load(n).get("project", "default") == p for n in networks())} for p in sorted(names)]


def project_status(project=None, log_lines=30):
    """Everything an agent needs to resume: the project's VMs and networks as they are now (want: what each should
    be doing), its notes, and the latest actions (with errors) from its log."""
    project = check_project(project or DEFAULT_PROJECT)
    d = project_dir(project)
    notes, log = "", []
    with contextlib.suppress(OSError):
        with open(os.path.join(d, "notes.md"), encoding="utf-8") as f:
            notes = f.read()
    with contextlib.suppress(OSError):
        with open(os.path.join(d, AGENT_LOG), encoding="utf-8") as f:
            log = [json.loads(l) for l in collections.deque(f, log_lines) if l.strip()]
    vms = [dict(v, want=wanted(v["name"])) for v in quiet(ls) if v.get("engine") is None and under(v["project"], project)]
    return {"project": project, **({"description": t} if (t := description("project", project)) else {}), "vms": vms,
            "networks": [dict(n, vms=[v for v in n["vms"] if under(project_of(load(v["vm"])), project)])
                         for n in net_ls() if under(n.get("project", "default"), project)],
            "sub_projects": sorted(p["project"] for p in projects() if p["project"].startswith(project + "/")),
            "notes": notes, "recent_actions": log}


# ---------- descriptions (phase 12) ----------
# One line on each VM, network and project (what it is for, who uses it), so an agent arriving knows who does what.
# One central file (terminal-ct keeps the same for its objects): changeable at any time, never inherited by a clone.
DESC_MAX = 200
DESC_KINDS = ("vm", "net", "project")
_DESC = {}  # (path, mtime, size) -> its content: read once per change, not once per VM listed


def check_description(text):
    if not isinstance(text, str) or len(text) > DESC_MAX or not text.isprintable():
        raise SystemExit(f"a description is one line of at most {DESC_MAX} printable characters")
    return text.strip()


def descriptions():
    """{"KIND:NAME": text}."""
    path = os.path.join(STORE, "descriptions.json")
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if key not in _DESC:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            _DESC.clear()
            _DESC[key] = d if isinstance(d, dict) else {}
        return _DESC[key]
    except (OSError, ValueError):
        return {}


def description(kind, name):
    return descriptions().get(f"{kind}:{name}", "")


def set_description(kind, name, text):
    """Set NAME's description; "" clears it. No check that NAME exists (describe does): rm uses it to forget."""
    text = check_description(text)
    with quota_lock():
        d = dict(descriptions())
        if text:
            d[f"{kind}:{name}"] = text
        elif d.pop(f"{kind}:{name}", None) is None:
            return {kind: name, "description": ""}
        path = os.path.join(STORE, "descriptions.json")
        with open(path + ".new", "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False)
        os.replace(path + ".new", path)
        _DESC.clear()  # same size within one clock tick would look unchanged
    return {kind: name, "description": text}


def describe(kind, name, text):
    """describe KIND NAME TEXT: the VM or network must exist (a project only needs a valid name)."""
    if kind not in DESC_KINDS:
        raise SystemExit(f"describe one of: {', '.join(DESC_KINDS)}")
    if kind == "project":
        check_project(name)
    elif kind == "net":
        net_load(name)
    elif name not in machines():
        raise SystemExit(f"no VM named {name!r} (terminal-vm ls)")
    return set_description(kind, name, text)


def notes_write(project, text, mode="append"):
    path = os.path.join(project_dir(project), "notes.md")
    with open(path, "a" if mode == "append" else "w", encoding="utf-8") as f:
        f.write((f"\n## {time.strftime('%Y-%m-%d %H:%M')}\n" if mode == "append" else "") + text.rstrip() + "\n")
    return {"project": project or DEFAULT_PROJECT, "notes": mode, "bytes": os.path.getsize(path)}


def project_move(name, project):
    """Move a VM to another project (its files stay where they are: only its label changes)."""
    m = load(name)
    m["project"] = check_project(project)
    save(name, m)
    return {"vm": name, "project": project}


def _stopped(name):
    load(name)
    if state(name) != "stopped" or disk_in_use(name):  # QMP unreachable is not proof: a running QEMU holds the disk
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


@locked_op
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
    if os.path.isdir(os.path.join(vm_dir(src), "answers")):  # but reset reinstalls unattended, as for SRC
        shutil.copytree(os.path.join(vm_dir(src), "answers"), os.path.join(d, "answers"))
    m.pop("networks", None)  # each NIC's MAC is per VM: attach the clone again
    if m.get("cloud"):
        cloud_seed(new, m)
    if m.get("template"):  # its own name, account, host keys, ids and ready file at its first start
        node_seed(new, m, fresh=True)
    if m.get("os") in GUEST_TOOLS_OS and not m.get("sandbox"):  # renamed at its first start (sandbox: no agent)
        m["rename"] = computer_name(new)
    save(new, m)
    print(json.dumps({"cloned": src, "to": new}))


@locked_op
def rm(name):
    _stopped(name)
    shutil.rmtree(vm_dir(name))  # its network memberships go with its machine.json
    set_description("vm", name, "")  # a new VM of this name says nothing it did not say itself
    _gc_bases()
    print(json.dumps({"removed": name}))


@locked_op
def reset(name):
    """Blank disk of the same size, so the next start installs again (Windows answers rewritten, account kept).
    Deletes the disk's snapshots too."""
    _stopped(name)
    disk = os.path.join(vm_dir(name), "disk.qcow2")
    info = subprocess.run([qemu_bin("qemu-img"), "info", "--output=json", disk], check=True, capture_output=True, text=True)
    m = load(name)
    base = (m["template"] if os.path.isfile(m["template"]) else node_template(m["image"])) if m.get("template") \
        else cloud_image(m["image"]) if m.get("cloud") else None
    subprocess.run([qemu_bin("qemu-img"), "create", "-f", "qcow2", *(["-F", "qcow2", "-b", base] if base else []),
                    disk + ".new", str(json.loads(info.stdout)["virtual-size"])],
                   check=True, stdout=subprocess.DEVNULL)
    os.replace(disk + ".new", disk)  # the old disk survives a failed create
    _gc_bases()
    m = load(name)
    m.pop("installed", None)
    m.pop("base", None)  # a blank disk of its own: no longer built on the VM it was cloned from
    if m.get("cloud"):  # its image again, nothing to install
        m["installed"] = True
        cloud_seed(name, m)
    if m.get("template"):
        m.update(installed=True, template=base)
        node_seed(name, m)
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


@locked_op
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
    with in_use(name):
        return _transfer(name, direction, src, dest)


def _transfer(name, direction, src, dest):
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
    wake(name)
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
    used = 0.0
    try:
        while True:
            if time.monotonic() - used > 30:  # someone watching: a use (the daemon's idle pause waits)
                used = time.monotonic()
                with contextlib.suppress(RuntimeError, OSError):
                    wake(name)
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


def download(url, dest, sha256, algo="sha256"):
    """HTTPS download to dest, kept only if its digest (SHA-256 by default) matches."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # one .part per process: two creates fetching the same image never write the same file
    digest, part, label = hashlib.new(algo), f"{dest}.{os.getpid()}.part", os.path.basename(dest)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            if urllib.parse.urlsplit(r.geturl()).scheme != "https":  # checked before any file is written
                raise SystemExit(f"{url} redirected off HTTPS ({r.geturl()}); aborting")
            with open(part, "wb") as f:
                total, done, shown = int(r.headers.get("Content-Length", 0)), 0, -1
                while chunk := r.read(1 << 20):
                    f.write(chunk); digest.update(chunk); done += len(chunk)
                    if total and done * 100 // total != shown:  # once per percent: a log keeps 100 lines
                        shown = done * 100 // total
                        print(f"\rdownloading {label}: {shown}%", end="", file=sys.stderr)
        print(file=sys.stderr)
        if digest.hexdigest() != sha256:
            raise SystemExit(f"checksum mismatch for {label}; download discarded")
        if not os.path.exists(dest):  # else another process got there first, with the same verified content
            os.replace(part, dest)
        return dest
    finally:  # interrupted, refused, or not needed: never hundreds of MB left behind
        with contextlib.suppress(OSError):
            os.remove(part)


# Official cloud images (already installed systems, set up at first boot by cloud-init), verified by the
# SHA-512 lists their projects publish next to them.
CLOUD_IMAGES = {
    "debian": "https://cloud.debian.org/images/cloud/trixie/latest/",
    "alpine": "https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/cloud/",
}


def cloud_image(kind):
    """The newest cloud image of that kind, cached in STORE/images (its name carries its digest); offline, the
    newest one cached."""
    folder = os.path.join(STORE, "images")
    try:
        index = CLOUD_IMAGES[kind]
        if kind == "debian":
            name = "debian-13-genericcloud-amd64.qcow2"
            sums = urllib.request.urlopen(index + "SHA512SUMS", timeout=30).read().decode()
            found = re.search(r"([0-9a-f]{128})\s+" + re.escape(name), sums)
            if not found:
                raise SystemExit(f"{name} is not in {index}SHA512SUMS")
            digest = found[1]
        else:
            listing = urllib.request.urlopen(index, timeout=30).read().decode()
            names = set(re.findall(r'href="(generic_alpine-([\d.]+)-x86_64-bios-cloudinit-r(\d+)\.qcow2)"', listing))
            if not names:
                raise SystemExit(f"no Alpine cloud image listed at {index}")
            name = max(names, key=lambda n: ([int(x) for x in n[1].split(".")], int(n[2])))[0]
            digest = urllib.request.urlopen(index + name + ".sha512", timeout=30).read().decode().split()[0]
            if not re.fullmatch(r"[0-9a-f]{128}", digest):
                raise SystemExit(f"unexpected checksum file for {name}")
    except OSError as e:  # no network: the newest cached image of that kind
        prefix = "debian-13-genericcloud" if kind == "debian" else "generic_alpine-"
        cached = sorted((f for f in os.listdir(folder) if f.startswith(prefix) and f.endswith(".qcow2")),
                        key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True) \
            if os.path.isdir(folder) else []
        if not cached:
            raise SystemExit(f"cannot fetch the {kind} cloud image ({e}) and none is cached") from None
        print(f"cannot check for a newer {kind} cloud image ({e}); using the cached {cached[0]}", file=sys.stderr)
        return _in_use_now(os.path.join(folder, cached[0]))
    dest = os.path.join(folder, f"{name[:-6]}-{digest[:12]}.qcow2")
    if os.path.exists(dest):
        return _in_use_now(dest)
    download(CLOUD_IMAGES[kind] + name, dest, digest, "sha512")
    os.chmod(dest, 0o444)  # the base of every VM made from it: never written
    _gc_cloud_images(dest)
    return dest


def _in_use_now(image):
    """Mark a cloud image as just handed out: a create is about to build on it (see _gc_cloud_images)."""
    with contextlib.suppress(OSError):  # shortcut: a read-only file's time cannot change on Windows; rare race left
        os.utime(image)
    return image


def _used_backings():
    """The files some VM disk or clone base is built on (normalised paths); None when a disk cannot be read: an
    image in use would look free."""
    disks = [os.path.join(STORE, n, "disk.qcow2") for n in os.listdir(STORE)]
    if os.path.isdir(os.path.join(STORE, ".bases")):  # a clone's source moved its disk there: the template's user
        disks += [os.path.join(STORE, ".bases", f) for f in os.listdir(os.path.join(STORE, ".bases"))]
    used = set()
    for disk in filter(os.path.isfile, disks):
        r = subprocess.run([qemu_bin("qemu-img"), "info", "-U", "--output=json", disk], capture_output=True, text=True)
        if r.returncode:
            return None
        try:
            backing = json.loads(r.stdout).get("backing-filename")
        except ValueError:
            return None
        if backing:
            used.add(os.path.normcase(os.path.abspath(backing)))
    return used


def _gc_cloud_images(keep):
    """Older cloud images no VM disk (or base) is built on any more: ~350 MB each, and Debian's latest changes often.
    Not one handed out in the last 10 minutes (its VM's disk may not exist yet), and nothing when a disk's backing
    cannot be read (an image in use would look free)."""
    folder, used = os.path.dirname(keep), _used_backings()
    if used is None:
        return
    for f in os.listdir(folder):
        path = os.path.normcase(os.path.abspath(os.path.join(folder, f)))
        if (f.startswith(("debian-13-genericcloud", "generic_alpine-")) and f.endswith(".qcow2")
                and path != os.path.normcase(os.path.abspath(keep)) and path not in used
                and time.time() - os.path.getmtime(path) > 600):
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
                os.remove(path)


def cloud_seed(name, m):
    """cloud-init's NoCloud seed of a cloud VM: a folder QEMU shows as a FAT disk labelled cidata. Its account,
    the guest agent, and for a node Docker and Python 3. A new instance id (clone, reset) makes cloud-init run
    again. The last step leaves m["ready_file"], named after this instance: a clone's disk holds its source's, not
    its own; and only once the agent (and for a node Docker) answers."""
    d, alpine = os.path.join(vm_dir(name), "cidata"), m["image"] == "alpine"
    host = name.replace("_", "-").strip("-") or "vm"
    m["instance_id"] = f"{host}-{secrets.token_hex(4)}"
    m["ready_file"] = f"/var/lib/terminal-vm/ready-{m['instance_id']}"
    packages = ["qemu-guest-agent"] + (["docker" if alpine else "docker.io", "python3"] if m.get("node") else [])
    services = ["qemu-guest-agent"] + (["docker"] if m.get("node") else [])
    start = [f"rc-update add {s} default && rc-service {s} start" if alpine else f"systemctl enable --now {s} || "
             f"systemctl start {s}" for s in services]
    if m.get("node"):
        if alpine:  # cloud-init runs before OpenRC started cgroups: Docker would refuse to start
            start.insert(1, "rc-service cgroups start || true")
        start.append("for i in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done")  # ready = answers
    user = {"name": m["user"], "shell": "/bin/ash" if alpine else "/bin/bash", "lock_passwd": False}
    if not alpine:  # Alpine's cloud image has doas, not sudo
        user["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
    config = {"hostname": host, "users": [user], "ssh_pwauth": False, "package_update": True, "packages": packages,
              "chpasswd": {"expire": False, "users": [{"name": m["user"], "password": m["password"], "type": "text"}]},
              "runcmd": start + [("docker info >/dev/null 2>&1 && " if m.get("node") else "")
                                 + f"mkdir -p /var/lib/terminal-vm && touch {m['ready_file']}"]}
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, "meta-data"), "w", newline="\n") as f:  # quoted: a name of digits stays a string
        f.write(f'instance-id: "{m["instance_id"]}"\nlocal-hostname: "{host}"\n')
    with open(os.path.join(d, "user-data"), "w", newline="\n") as f:
        f.write("#cloud-config\n" + json.dumps(config, indent=1) + "\n")  # JSON is YAML: no quoting to get wrong


# ---------- node templates (K8): a node boots in seconds, not minutes ----------
# A node made from the cloud image ran cloud-init at every boot (~35 s of its ~65 s, nested KVM in WSL) and its
# first boot installed Docker from the internet (~90 s). A template is that VM set up once (Docker, Python, the
# guest agent), then slimmed: cloud-init and the NTP daemon off (sync_clock sets the time), DHCP without its ARP
# probe and IPv6 wait, no boot menu delay, services started in parallel; its account, host keys and ids removed.
# Each node is an overlay on it; its identity (host name, account, password, disk size) is set by the guest agent
# at its first start (_identity). Measured (WSL): agent answers in ~11 s, Docker in ~12 s, from 63-66 s.
NODE_TEMPLATE_V = 1  # raise when the slimming changes: a new template is built from the cloud image
NODE_SLIM = {
    "alpine": r"""set -e
for s in cloud-init-local cloud-init cloud-config cloud-final cloud-init-hotplugd chronyd; do
  for l in boot default; do rc-update del $s $l 2>/dev/null || true; done
done
printf 'auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet dhcp\n' > /etc/network/interfaces
grep -q '^noarp' /etc/dhcpcd.conf || printf 'noarp\nipv4only\nnoipv6rs\n' >> /etc/dhcpcd.conf
sed -i 's/^TIMEOUT .*/TIMEOUT 1/' /boot/extlinux.conf
sed -i '/^rc_parallel=/d' /etc/rc.conf; echo 'rc_parallel="YES"' >> /etc/rc.conf
deluser --remove-home user 2>/dev/null || true
""",
    "debian": r"""set -e
touch /etc/cloud/cloud-init.disabled
systemctl disable chrony.service systemd-timesyncd.service 2>/dev/null || true
systemctl mask systemd-networkd-wait-online.service 2>/dev/null || true
if [ -f /etc/default/grub ]; then sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub; update-grub 2>/dev/null || true; fi
userdel -r user 2>/dev/null || true; rm -f /etc/sudoers.d/90-cloud-init-users
: > /etc/machine-id; rm -f /var/lib/dbus/machine-id
""",
}
NODE_SLIM_COMMON = r"""
rm -rf /var/lib/terminal-vm /etc/ssh/ssh_host_* /root/.ash_history /root/.bash_history /var/lib/docker/engine-id
cloud-init clean --logs --seed 2>/dev/null || true
sync
"""


def node_template(kind):
    """The node template of that kind for the newest cloud image (built once, ~2 min with the internet): its path,
    a read-only qcow2 in STORE/images."""
    base = cloud_image(kind)
    folder = os.path.dirname(base)
    dest = os.path.join(folder, f"node-{os.path.basename(base)[:-6]}-t{NODE_TEMPLATE_V}.qcow2")
    lock, end = dest + ".lock", time.monotonic() + 3600  # longer than the slowest build (setup 15 min and more)
    while True:  # one build at a time: two node add at once wait for the same template
        if os.path.exists(dest):
            return _in_use_now(dest)
        try:
            os.mkdir(lock)
            with open(os.path.join(lock, "pid"), "w") as f:
                f.write(str(os.getpid()))
            break
        except FileExistsError:
            if not _builder_alive(lock):  # its build was killed (or the host restarted): take over
                stale = f"{lock}.{os.getpid()}.stale"
                with contextlib.suppress(OSError):
                    os.rename(lock, stale)  # of two seeing it dead, one claims it
                    shutil.rmtree(stale, ignore_errors=True)
                continue
            if time.monotonic() > end:
                raise SystemExit(f"another command is still building the {kind} node template; try again")
            time.sleep(1)
    for n in machines():  # left by a killed build of this kind (the lock was free: no build of it runs)
        if n.startswith(f"tmpl-{kind}-"):
            with contextlib.suppress(OSError, SystemExit), captured(io.StringIO()):
                if state(n) != "stopped":
                    stop(n, force=True)
                rm(n)
    tmp = f"tmpl-{kind}-{secrets.token_hex(3)}"
    try:
        if os.path.exists(dest):
            return _in_use_now(dest)
        print(f"building the {kind} node template once (about 2 min, needs the internet)", file=sys.stderr)
        with captured(io.StringIO()):
            create(tmp, None, image=kind, node=True, cpus=min(2, os.cpu_count()), memory=1, max_memory=1, disk=4,
                   template=False, base_image=base)  # 1 GiB, no balloon headroom; the image dest names
            start(tmp, set_want=False)
        ready, end = load(tmp)["ready_file"], time.monotonic() + 900
        while True:
            with contextlib.suppress(OSError, SystemExit):
                if guest_run(tmp, ["test", "-f", ready], 10)["exit"] == 0:
                    break
            if state(tmp) == "stopped" or time.monotonic() > end:
                raise SystemExit(f"the {kind} node template did not finish its setup (it needs the internet); "
                                 f"see {os.path.join(vm_dir(tmp), 'qemu.log')}")
            time.sleep(1)
        r = guest_run(tmp, ["/bin/sh", "-c", NODE_SLIM[kind] + NODE_SLIM_COMMON], 300)
        if r["exit"]:
            raise SystemExit(f"slimming the {kind} node template failed: {(r['stderr'] or r['stdout'])[-300:]}")
        with captured(io.StringIO()):
            try:
                stop(tmp, timeout=120)
            except SystemExit:  # a guest ignoring ACPI: its writes were synced
                stop(tmp, force=True)
        part = f"{dest}.{os.getpid()}.part"
        subprocess.run([qemu_bin("qemu-img"), "convert", "-O", "qcow2", os.path.join(vm_dir(tmp), "disk.qcow2"), part],
                       check=True, stdout=subprocess.DEVNULL)  # standalone: the cloud image may be collected later
        os.chmod(part, 0o444)
        os.replace(part, dest)
        _gc_node_templates(dest)
        return _in_use_now(dest)
    finally:
        with contextlib.suppress(OSError, SystemExit), captured(io.StringIO()):
            if os.path.isdir(vm_dir(tmp)):
                if state(tmp) != "stopped":
                    stop(tmp, force=True)
                rm(tmp)
        for f in os.listdir(folder):
            if f.startswith(os.path.basename(dest) + ".") and f.endswith(".part"):
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(folder, f), 0o600)  # read-only: Windows refuses to remove it
                    os.remove(os.path.join(folder, f))
        shutil.rmtree(lock, ignore_errors=True)


def _builder_alive(lock):
    """Whether the process that holds a template build lock still runs (no pid yet: being taken, for a minute)."""
    try:
        with open(os.path.join(lock, "pid")) as f:
            pid = int(f.read())
    except FileNotFoundError:
        try:
            return time.time() - os.path.getmtime(lock) < 60
        except OSError:
            return False
    except (OSError, ValueError):
        return False
    if WINDOWS:
        return _proc_created(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's process: alive
        return True
    return True


def _gc_node_templates(keep):
    """Older node templates of the same kind that no VM disk or clone base is built on any more; not one handed out
    in the last 10 minutes (a create or reset may be about to build on it), nothing when a disk cannot be read."""
    folder, kind = os.path.dirname(keep), os.path.basename(keep).split("-")[1]
    prefix = "node-debian-13-genericcloud" if kind == "debian" else "node-generic_alpine-"
    used = _used_backings()
    if used is None:
        return
    for f in os.listdir(folder):
        path = os.path.normcase(os.path.abspath(os.path.join(folder, f)))
        if f.startswith(prefix) and f.endswith(".qcow2") and path != os.path.normcase(os.path.abspath(keep)) \
                and path not in used and time.time() - os.path.getmtime(path) > 600:
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
                os.remove(path)


def node_identity_script(name, m):
    """Run as root by the guest agent at a template node's first start: its own name, account, disk size; then its
    ready file once it has a route and Docker answers. Idempotent (a retry after a cut start)."""
    host = name.replace("_", "-").strip("-") or "vm"
    alpine = m["image"] == "alpine"
    user = shlex.quote(m["user"])
    add = (f"id {user} >/dev/null 2>&1 || adduser -D -s /bin/ash {user}" if alpine else
           f"id {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}; "
           f"echo {shlex.quote(m['user'] + ' ALL=(ALL) NOPASSWD:ALL')} > /etc/sudoers.d/90-terminal-vm; "
           f"chmod 440 /etc/sudoers.d/90-terminal-vm")
    # a clone's disk holds its source's host keys and ids: new ones, then the services that read them restarted
    fresh = bool(m.get("fresh_ids"))
    fresh_rm = ("rm -f /etc/ssh/ssh_host_* /var/lib/docker/engine-id; : > /etc/machine-id; "
                "rm -f /var/lib/dbus/machine-id\n") if fresh else ""
    fresh_restart = (("rc-service docker restart >/dev/null 2>&1 || true; rc-service sshd restart >/dev/null 2>&1 "
                      "|| true\n") if alpine else "systemctl restart docker ssh >/dev/null 2>&1 || true\n") if fresh else ""
    return f"""set -e
IFS= read -r pw  # on stdin: never in the guest's process list
echo {host} > /etc/hostname; hostname {host}
sed -i '/^127[.]0[.]1[.]1[[:space:]]/d' /etc/hosts; echo "127.0.1.1 {host}" >> /etc/hosts
{add}
printf '%s:%s\\n' {user} "$pw" | chpasswd
{fresh_rm}[ -s /etc/machine-id ] || {{ command -v systemd-machine-id-setup >/dev/null && systemd-machine-id-setup >/dev/null; }} || true
ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || ssh-keygen -A >/dev/null 2>&1 || true
{fresh_restart}
dev=$(awk '$2 == "/" {{print $1}}' /proc/mounts | tail -1); dev=$(readlink -f "$dev")
case "$dev" in
  *[0-9]) disk=$(echo "$dev" | sed 's/p\\?[0-9]*$//'); [ -b "$disk" ] && [ "$disk" != "$dev" ] && \\
          {{ growpart "$disk" "${{dev##*[!0-9]}}" >/dev/null 2>&1 || true; }} ;;
esac
resize2fs "$dev" >/dev/null 2>&1 || true
for i in $(seq 60); do ip route 2>/dev/null | grep -q '^default' && break; sleep 0.5; done
for i in $(seq 120); do docker info >/dev/null 2>&1 && break; sleep 0.5; done
docker info >/dev/null 2>&1
mkdir -p /var/lib/terminal-vm && touch {shlex.quote(m['ready_file'])}
"""


def node_identity(name, wait=600):
    """Started by start for a template node's first run: once the guest agent answers, give the guest its identity
    (node_identity_script, its account's password on stdin), then forget the step."""
    end, said = time.time() + wait, None
    while time.time() < end and state(name) != "stopped":
        m = load(name)
        try:
            with in_use(name):  # holds the agent's one connection: a use (no idle pause, no health miss)
                r = guest_exec(name, "/bin/sh", ["-c", node_identity_script(name, m)], 180,
                               stdin=(m["password"] + "\n").encode())
            if r["exit"] == 0:
                with vm_op(name):
                    m = load(name)
                    for k in ("identity", "fresh_ids"):
                        m.pop(k, None)
                    save(name, m)
                return
            err = (r["stderr"] or r["stdout"])[-300:]
            if err != said:  # once per distinct failure (qemu.log), retried until wait
                print(f"{name}: identity step failed: {err}", file=sys.stderr, flush=True)
                said = err
        except (OSError, ValueError, TypeError, KeyError):  # agent not up yet, or busy with another client
            pass
        time.sleep(1)
    if state(name) != "stopped":  # never ready, and why: in the events, not only in qemu.log
        event("identity_failed", name, said or f"guest agent silent for {wait} s")


def node_seed(name, m, fresh=False):
    """A template node's own identity, set at its next start (node_identity): a new ready file (a clone's disk holds
    its source's)."""
    host = name.replace("_", "-").strip("-") or "vm"
    m["instance_id"] = f"{host}-{secrets.token_hex(4)}"
    m["ready_file"] = f"/var/lib/terminal-vm/ready-{m['instance_id']}"
    m["identity"] = True
    if fresh:  # a clone: new host keys and ids too
        m["fresh_ids"] = True


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


# ---------- bench: timings of terminal-vm's own operations ----------

BENCH_FROM = "t2"  # a small stopped VM (TinyCore) to clone


def _timed(f, *a, **k):
    t = time.perf_counter()
    quiet(f, *a, **k)
    return (time.perf_counter() - t) * 1000


def _summary(ms):
    ms = sorted(ms)
    return {"median_ms": round(ms[len(ms) // 2] if len(ms) % 2 else (ms[len(ms) // 2 - 1] + ms[len(ms) // 2]) / 2, 1),
            "min_ms": round(ms[0], 1), "max_ms": round(ms[-1], 1), "runs": len(ms)}


def _stop_wait(name, timeout=30):
    """Forced stop, until QEMU is gone (stop --force only sends the signal)."""
    quiet(stop, name, force=True)
    end = time.monotonic() + timeout
    while state(name) != "stopped" or disk_in_use(name):  # Windows: TerminateProcess ends QEMU a bit later
        if time.monotonic() > end:
            raise SystemExit(f"{name}: still running {timeout} s after a forced stop")
        time.sleep(0.1)


def bench(runs=10, src=None):
    """Time terminal-vm's own operations: median, min and max of `runs` cycles on a copy-on-write clone of SRC
    (a stopped VM; default t2): clone, start, screenshot, pause, resume, the listing, the dashboard's data, forced
    stop, rm. SRC is never started; cloning makes its disk an overlay on a shared base, as any clone does. The
    bench-* clone is removed after; the quota applies as for any start."""
    src = src or BENCH_FROM
    if load(src).get("os", "linux") != "linux":  # a Windows clone is renamed by a long guest session at start
        raise SystemExit(f"bench clones a Linux VM; {src} is not one (--from VM)")
    _stopped(src)
    times, name = collections.defaultdict(list), f"bench-{os.getpid()}-{secrets.token_hex(2)}"
    created = False  # only bench's own clone is ever stopped or removed
    try:
        for _ in range(runs):
            times["clone"].append(_timed(clone, src, name))
            created = True
            times["start"].append(_timed(start, name, set_want=False))  # not supervised: no restart in between
            times["screenshot"].append(_timed(screenshot, name, file="bench.png"))
            times["pause"].append(_timed(pause, name))
            times["resume"].append(_timed(pause, name, resume=True))
            times["ls"].append(_timed(vm_list))
            times["dashboard_data"].append(_timed(_vm_rows))
            times["stop_force"].append(_timed(_stop_wait, name))
            times["rm"].append(_timed(rm, name))
            created = False
    finally:
        if created:  # the step's own error goes on; cleanup errors (SystemExit too) must not hide it
            with contextlib.suppress(Exception, SystemExit):
                if state(name) != "stopped" or disk_in_use(name):
                    _stop_wait(name)
            with contextlib.suppress(Exception, SystemExit):
                quiet(rm, name)
    qemu = None
    with contextlib.suppress(OSError, SystemExit, subprocess.SubprocessError):
        qemu = subprocess.run([qemu_bin("qemu-system-x86_64"), "--version"], capture_output=True, text=True,
                              **NO_WINDOW).stdout.split("\n")[0].strip()
    return {"tool": f"terminal-vm {VERSION}", "qemu": qemu, "accel": " ".join(a for a in accelerators()[0] if a != "-accel"),
            "host": platform.platform() + (" (WSL)" if in_wsl() else ""), "date": time.strftime("%Y-%m-%d"),
            "from": f"{src} ({load(src).get('os', 'linux')})", "results": {k: _summary(v) for k, v in times.items()}}


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
    with captured(buf):
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


def in_scope(name, project):
    """A server with a project reaches nothing outside it (and its sub-projects), approval or not."""
    if not under(project_of(load(name)), project):  # the same answer as for no VM: other projects' names stay unseen
        raise SystemExit(f"no VM named {name!r}; create it with: terminal-vm up {name}")


def scoped_quota(project):
    """The room left and this project's own use: not what the other projects take."""
    q, use = quota(), {"cpus": 0, "memory_gb": 0, "vms": 0}
    for n in machines():
        m = load(n)
        if under(project_of(m), project) and state(n) != "stopped":
            use["cpus"] += run_cpus(n, m); use["memory_gb"] += vm_ram_mb(m) / 1024; use["vms"] += 1
    return {k: {"limit": v["limit"], "free": v["free"], **({"used_by_project": round(use[k], 1)} if k in use else {})}
            for k, v in q.items()}


def agent_describe(kind, name, text, approve, project=None):
    """describe through MCP: free on the agent's own VMs and networks and on its server's project (and below); the
    user's need their approval. Outside the agent's project: not found."""
    text = check_description(text)
    if not isinstance(name, str) or kind not in DESC_KINDS:
        raise SystemExit(f"kind is one of {', '.join(DESC_KINDS)}, and name a string")
    cli = f"terminal-vm describe {kind} {shlex.quote(name)} " + (shlex.quote(text) if text else "--clear")
    ask = lambda what: approve(f"Let the agent describe {what} as: {text!r}?" if text else
                               f"Let the agent clear the description of {what}?", cli)
    if kind == "vm":
        if name not in machines():
            raise SystemExit(f"no VM named {name!r}; create it with: terminal-vm up {name}")
        in_scope(name, project)
        if not agent_owned(name):
            ask(f"your VM {name}")
    elif kind == "net":
        if name not in networks() or not under(net_load(name).get("project", "default"), project):
            raise SystemExit(f"no network named {name!r}; create it with: terminal-vm net create {name}")
        if not _net_owned(name):
            ask(f"your network {name}")
    else:
        if not under(check_project(name), project):
            raise SystemExit(f"project {name!r} is not this agent's project {project!r} or below it")
        if not project:
            ask(f"the project {name}")
    return describe(kind, name, text)


def agent_call(tool, a, approve, project=None):
    """project: the server's own (--project); then nothing outside it is visible or reachable."""
    vm, net = a.get("vm"), a.get("net")
    asked = a.get("project")  # where a creation or a note goes: this server's project or below it
    if asked is not None and not under(check_project(asked), project):
        raise SystemExit(f"project {asked!r} is not this agent's project {project!r} or below it")
    where = asked or project
    if project and vm and tool != "vm_create":  # reads too: another project's VMs are not this agent's business
        in_scope(vm, project)
    if project and net and tool != "net_create" and net in networks() and \
            not under(net_load(net).get("project", "default"), project):
        raise SystemExit(f"no network named {net!r}; create it with: terminal-vm net create {net}")
    if tool == "vm_list":
        return [dict(v, managed=v.get("engine") is None and bool(load(v["name"]).get("sandbox") or agent_owned(v["name"])))
                for v in quiet(ls) if not project or v.get("engine") is None and under(v["project"], project)]
    if tool == "project_status":
        return project_status(where)
    if tool == "notes_write":
        return notes_write(where, a["text"], a.get("mode", "append"))
    if tool == "autostart":
        if vm:  # inside the guest: terminal-ct starts its containers again when the VM boots
            if a.get("on") is None:
                raise SystemExit("inside a VM: give on (true or false)")
            if PROFILES[load(vm).get("os", "linux")]["family"] != "linux":
                raise SystemExit("autostart inside a VM is for terminal-ct in Linux guests")
            users = guest_users(vm)
            if users and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", users[0]):
                raise SystemExit(f"{vm}: unexpected user name {users[0]!r} reported by the guest")
            if not users:
                raise SystemExit(f"{vm} has no logged-in user to run terminal-ct as: sign in first (terminal-vm view {vm})")
            what = "on" if a["on"] else "off"
            approve(f"Inside {vm}, turn terminal-ct's autostart {what} (its containers start again when the VM boots)?",
                    f"terminal-ct autostart {what}   (in {vm}, as {users[0]})")
            r = guest_shell(vm, f"runuser -l {shlex.quote(users[0])} -c 'XDG_RUNTIME_DIR=/run/user/$(id -u) "
                                f"terminal-ct autostart {what}'")
            if r["exit"]:
                raise SystemExit(f"in {vm}: {(r['stderr'] or r['stdout']).strip() or 'failed'} "
                                 "(is terminal-ct installed in the guest, on PATH?)")
            return {"vm": vm, "inside": json.loads(r["stdout"].strip().splitlines()[-1])}
        if a.get("on") is not None:
            approve(("Start the VMs that were running (in every project) again by themselves after each restart of "
                     "the PC (a task at your logon)?") if a["on"] else "Stop restarting VMs after a restart of the PC?",
                    f"terminal-vm autostart {'on' if a['on'] else 'off'}")
        return autostart(a.get("on"))
    if tool == "describe":
        return agent_describe(a.get("kind"), a.get("name"), a.get("description", ""), approve, project)
    desc = check_description(a.get("description") or "") if tool in ("vm_create", "vm_clone", "net_create") else ""
    if tool in ("vm_create", "vm_clone", "net_create"):
        taken = (a.get("new") if tool == "vm_clone" else vm) if tool != "net_create" else None
        if taken is not None and os.path.exists(os.path.join(vm_dir(taken), "machine.json")) or \
                tool == "net_create" and net in networks():
            raise SystemExit(f"the name {taken or net!r} is not available: choose another")  # same answer, any project
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
                    offline=not sandbox, sandbox=sandbox, max_cpus=a.get("max_cpus"), max_memory=mm, project=where)
        m = load(vm); m["created_by"] = "agent"; save(vm, m)
        if desc:
            set_description("vm", vm, desc)
        return out
    if tool == "vm_clone":
        src = load(vm)
        users = not (src.get("sandbox") or src.get("created_by") == "agent")
        if users:  # a copy of their disk and account
            approve(f"Clone your VM {vm} as {a['new']} (an offline copy of its disk and account the agent manages)?",
                    f"terminal-vm clone {vm} {a['new']}")
        out = quiet(clone, vm, a["new"])
        m = load(a["new"]); m["created_by"] = "agent"
        if where:
            m["project"] = where
        if users and not m.get("sandbox"):
            m["offline"] = True  # internet stays the user's call (vm_internet)
        save(a["new"], m)
        if desc:
            set_description("vm", a["new"], desc)
        return out
    if tool == "net_list":
        return [dict(n, vms=[v for v in n["vms"] if under(project_of(load(v["vm"])), project)])
                for n in net_ls() if under(n.get("project", "default"), project)]
    if tool == "net_create":
        cfg = net_create(net, where)
        with open(net_file(net), "w") as f:
            json.dump(dict(cfg, created_by="agent"), f)
        if desc:
            set_description("net", net, desc)
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
    ("exec", "Run a shell command in the guest through its guest agent (Linux: sh -c as root; Windows: PowerShell as "
             "SYSTEM) and return its exit code, stdout and stderr. Not in sandboxes (no guest agent).",
     _schema({"command": {"type": "string"}, "timeout": _N}, ["command"])),
]
INPUT_TOOLS = {"click", "move", "drag", "scroll", "type", "key"}
_VM, _NET = {"vm": {"type": "string"}}, {"net": {"type": "string"}}
_MANAGED = " Agents manage sandboxed VMs and the VMs they created; the user's other VMs are off-limits."
_DESC_ARG = {"description": {"type": "string", "description": "what it is for, one line (who uses it, its role)"}}
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
              "sandbox": {"type": "boolean"}, "iso": {"type": "string"}, **_DESC_ARG}, ["vm"])),
    ("vm_clone", "Copy-on-write clone of a stopped VM, in seconds (install once, clone many). Both stay usable. Cloning one of the user's VMs needs their approval; the clone starts offline.",
     _schema({**_VM, "new": {"type": "string"}, **_DESC_ARG}, ["vm", "new"])),
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
    ("net_create", "Create an isolated VM-to-VM network (no LAN, no internet, no DHCP).",
     _schema({**_NET, **_DESC_ARG}, ["net"])),
    ("describe", "Set what an object is for, in one line (at most 200 characters; \"\" clears it), so the next agent "
                 "knows who does what: a VM, a network or a project. Free on the agent's own VMs and networks (and "
                 "its own project); the user's need their approval.",
     _schema({"kind": {"type": "string", "enum": list(DESC_KINDS)}, "name": {"type": "string"},
              "description": {"type": "string"}}, ["kind", "name", "description"])),
    ("net_remove", "Remove an empty network this agent created.", _schema(_NET, ["net"])),
    ("net_attach", "Plug a stopped managed VM into a network; set the suggested address in the guest. A network "
                   "the user created needs their approval.", _schema({**_VM, **_NET}, ["vm", "net"])),
    ("net_detach", "Unplug a stopped managed VM from a network.", _schema({**_VM, **_NET}, ["vm", "net"])),
    ("autostart", "Show (without on) or change whether the VMs that were running start again by themselves after "
                  "the PC restarts (a task at the user's logon). With vm: inside that Linux VM, turn terminal-ct's "
                  "autostart on or off (its containers start again when the VM boots). Changing needs the user's "
                  "approval.", _schema({"on": {"type": "boolean"}, **_VM})),
    ("project_status", "Start here: the project's VMs and networks as they are now (want: what each should be "
                       "doing), the notes left by earlier sessions, and the latest actions with their errors.",
     _schema({"project": {"type": "string", "description": "Only when this server has no project of its own"}})),
    ("notes_write", "Keep notes for the next session (what is set up, what is left to do): appended with the date, "
                    "or mode=replace to rewrite them.",
     _schema({"text": {"type": "string"}, "mode": {"type": "string", "enum": ["append", "replace"]},
              "project": {"type": "string"}}, ["text"])),
]
AGENT_NAMES = {n for n, _, _ in AGENT_TOOLS}
PROJECT_ARG_TOOLS = {"vm_create", "vm_clone", "net_create", "project_status", "notes_write"}
INSTRUCTIONS = ("terminal-vm: VMs with a desktop for computer-use agents. Call project_status first: it says what "
                "this project already has and what earlier sessions did and noted. Before you stop, record with "
                "notes_write what you set up and what is left, so the next session can pick up from there.")
_OTHER = {"vm": {"type": "string", "description": "Another VM this agent manages (default: this server's VM)"}}
SCREEN_TOOLS = [(n, d, s if n in ("top", "quota") else dict(s, properties={**s["properties"], **_OTHER}))
                for n, d, s in TOOLS]


def image(data):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"}


def mcp(name, project=None):
    """project: the agent sees and reaches only that project's VMs and networks (and its sub-projects'), and its
    notes and log are that project's."""
    if project:
        check_project(project)
        if not os.path.isfile(os.path.join(vm_dir(name), "machine.json")) or not under(project_of(load(name)), project):
            name = None  # its VM (by default "default") is outside the project: serve without one of its own
    shell_ok = set()  # VMs whose root shell (exec) the user approved in this session
    agents = {}  # per VM: whether qemu-guest-agent answered once; unknown until asked
    seen = {}  # per VM: when a logged-in session was last confirmed
    can_ask, pending, asks = False, collections.deque(), iter(range(1, 1 << 30))

    def record(tool, a, vm, error=None):  # sandbox screen actions, and every infrastructure action, for review
        try:
            m = {}
            with contextlib.suppress(SystemExit):
                m = load(vm) if vm else {}
            # the log of the project the action belongs to: this server's, else the one a creation names, else the
            # VM's own; never one an agent merely names (it would forge another project's history)
            p = project or (a.get("project") if tool in PROJECT_ARG_TOOLS else None) or (m and project_of(m))
            p = p if isinstance(p, str) and _PROJECT.fullmatch(p) and under(p, project) else project or DEFAULT_PROJECT
            mine = under(project_of(m), project)
            path = os.path.join(project_dir(p), AGENT_LOG) if tool in AGENT_NAMES or tool == "exec" else \
                os.path.join(vm_dir(vm), "actions.log") if m.get("sandbox") and mine else None
            if path:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": tool, "arguments": a,
                                        **({"vm": vm} if vm else {}), **({"error": error} if error else {})}) + "\n")
        except (OSError, TypeError, ValueError, SystemExit):  # a log that cannot be written must not end the server
            pass

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
                "capabilities": {"tools": {}}, "serverInfo": {"name": "terminal-vm", "version": VERSION},
                "instructions": INSTRUCTIONS + (f" This server's project: {project}." if project else "")}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, d, s in SCREEN_TOOLS + AGENT_TOOLS]}})
        elif method == "tools/call":
            p = req.get("params", {})
            tool, a = p.get("name"), dict(p.get("arguments") or {})
            vm = a.get("vm") if tool in AGENT_NAMES else a.pop("vm", None) or name
            try:
                if not vm and tool not in AGENT_NAMES and tool not in ("top", "quota"):
                    raise ValueError("name the VM (vm): this server has no VM of its own in its project")
                if project and tool not in AGENT_NAMES and tool not in ("top", "quota"):
                    in_scope(vm, project)  # every call: a VM moved to another project leaves this agent's reach
                if vm != name and tool not in AGENT_NAMES:  # screen tools on another VM: only one this agent manages
                    managed(vm)
                if tool == "exec" and vm not in shell_ok:  # root (SYSTEM) in the guest: more than its screen
                    if load(vm).get("created_by") != "agent":
                        approve(f"Let the agent run commands as root (SYSTEM) inside your VM {vm}?",
                                f"terminal-vm mcp {vm}   (or run the commands yourself)")
                    shell_ok.add(vm)
                # one guest-agent round trip per burst of input, not per click: a session seen < 5 s ago still holds
                if tool in INPUT_TOOLS and agents.get(vm) is not False and time.time() - seen.get(vm, 0) > 5:
                    users = guest_users(vm)
                    agents[vm] = users is not None
                    seen[vm] = time.time() if users else 0
                    if users == []:
                        raise RuntimeError("The guest has no logged-in session (login screen). Ask the user to sign in: "
                                           f"terminal-vm view {vm}")
                # the screen before input, to return the screen after its effect rather than a fixed delay later
                before = shot_pixels(vm) if a.get("screenshot") and tool in INPUT_TOOLS else None
                # resources opens its own QMP session (QEMU takes one client at a time), so not through act()
                out = (agent_call(tool, a, approve, project) if tool in AGENT_NAMES else
                       resources(vm, a.get("cpus"), a.get("memory_gb")) if tool == "resources" else
                       (scoped_quota(project) if project else quota()) if tool == "quota" else
                       (lambda t: dict(t, vms=[v for v in t["vms"] if under(project_of(load(v["name"])), project)],
                                       quota=scoped_quota(project)) if project else t)(top())
                       if tool == "top" else
                       guest_shell(vm, a["command"], max(1.0, min(float(a.get("timeout", 60)), 600))) if tool == "exec" else
                       act(vm, tool, a))
                record(tool, a, vm)
                if tool == "screenshot":
                    content = [image(out[0])]
                else:
                    content = [{"type": "text", "text": json.dumps(out)}]
                    if a.get("screenshot") or tool == "wait":
                        content.append(image(settled_screenshot(vm, before)))
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
    if (sys.argv[1:] if argv is None else argv)[:1] == ["_watch"]:  # internal: started by start for each VM
        return watch((sys.argv[2:] if argv is None else argv[1:])[0])
    if (sys.argv[1:] if argv is None else argv)[:1] == ["_rename"]:  # internal: started by start for a Windows clone
        return rename_guest((sys.argv[2:] if argv is None else argv[1:])[0])
    if (sys.argv[1:] if argv is None else argv)[:1] == ["_identity"]:  # internal: a template node's first start
        return node_identity((sys.argv[2:] if argv is None else argv[1:])[0])
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
        sp.add_argument("--project", help="project (sub-projects as lab/web; default TERMINAL_VM_PROJECT or default)")
    u = sub.add_parser("up"); u.add_argument("name", nargs="?", default="default"); u.add_argument("--iso")
    u.add_argument("--no-view", action="store_true"); u.add_argument("--plan", action="store_true"); sizes(u)
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("--iso"); sizes(c)
    c.add_argument("--image", choices=["debian", "alpine"], help="an official cloud image instead of an ISO")
    c.add_argument("--node", action="store_true", help="with --image: Docker and Python 3, a node for terminal-ct")
    c.add_argument("--description", help="what it is for (one line)")
    s = sub.add_parser("start"); s.add_argument("name"); s.add_argument("--window", action="store_true")
    t = sub.add_parser("stop"); t.add_argument("name"); t.add_argument("--force", action="store_true")
    t.add_argument("--timeout", type=int, default=60)
    for n in ("status", "hide", "pause", "rm", "reset"):
        sub.add_parser(n).add_argument("name")
    re_ = sub.add_parser("resume", help="NAME: un-pause it; no NAME: start again what ran before a restart")
    re_.add_argument("name", nargs="?"); re_.add_argument("--stay", action="store_true")
    sub.add_parser("autostart").add_argument("state", nargs="?", choices=["on", "off"])
    pj = sub.add_parser("project"); pj.add_argument("project_cmd", choices=["ls", "show", "notes", "move"])
    pj.add_argument("args", nargs="*")
    for n in ("view", "show"):
        v = sub.add_parser(n); v.add_argument("name"); v.add_argument("--read-only", action="store_true")
    sub.add_parser("ls"); sub.add_parser("doctor"); sub.add_parser("help"); sub.add_parser("version")
    cl = sub.add_parser("clone"); cl.add_argument("src"); cl.add_argument("new")
    cl.add_argument("--description", help="what the new one is for (one line)")
    ds = sub.add_parser("describe", help="KIND NAME [TEXT]: one line on what it is for (no TEXT: show it)")
    ds.add_argument("desc_kind", choices=DESC_KINDS); ds.add_argument("desc_name")
    ds.add_argument("desc_text", nargs="?"); ds.add_argument("--clear", action="store_true")
    sn = sub.add_parser("snapshot"); sn.add_argument("name"); sn.add_argument("snap_cmd", choices=["save", "load", "rm", "ls"])
    sn.add_argument("tag", nargs="?")
    di = sub.add_parser("display"); di.add_argument("name")
    di.add_argument("--width", type=int, required=True); di.add_argument("--height", type=int, required=True)
    im = sub.add_parser("image"); im.add_argument("--os", dest="os_name", default="linux", choices=list(PROFILES))
    im.add_argument("--plan", action="store_true")
    for n in ("push", "pull"):
        x = sub.add_parser(n); x.add_argument("name"); x.add_argument("src"); x.add_argument("dest")
    m = sub.add_parser("mcp"); m.add_argument("name", nargs="?", default="default"); m.add_argument("--project")
    k = sub.add_parser("skills"); k.add_argument("skill_cmd", choices=["get", "path", "install"]); k.add_argument("--dir")
    a = sub.add_parser("action"); a.add_argument("name"); a.add_argument("tool"); a.add_argument("args", nargs="*")
    a.add_argument("--output"); a.add_argument("--button", default="left"); a.add_argument("--double", action="store_true")
    a.add_argument("--dy", type=int); a.add_argument("--x", type=float); a.add_argument("--y", type=float)
    a.add_argument("--timeout", type=float, default=30); a.add_argument("--stable", type=float, default=1)
    a.add_argument("--layout", choices=sorted(LAYOUTS), help="this call only: us (QWERTY) or fr (AZERTY)")
    a.add_argument("--no-wake", action="store_true", help="screenshot: only look (a paused VM stays paused)")
    lo = sub.add_parser("layout"); lo.add_argument("name"); lo.add_argument("layout", nargs="?", choices=sorted(LAYOUTS))
    sb = sub.add_parser("sandbox"); sb.add_argument("name"); sb.add_argument("value", nargs="?", choices=["on", "off"])
    sub.add_parser("top")
    sub.add_parser("daemon").add_argument("daemon_cmd", choices=["run", "start", "stop", "status"])
    su = sub.add_parser("supervise"); su.add_argument("name"); su.add_argument("--restart", choices=["always", "never"])
    su.add_argument("--idle-pause", type=int, help="minutes unused (and under 20%% CPU) before the daemon pauses it (0: never)")
    ev = sub.add_parser("events"); ev.add_argument("-n", type=int, default=50); ev.add_argument("--vm")
    wb = sub.add_parser("web"); wb.add_argument("--project"); wb.add_argument("--no-open", action="store_true")
    wb.add_argument("web_args", nargs="*", help="user add NAME | user ls | user rm NAME")
    wb.add_argument("--role", choices=["read", "full"], default="full"); wb.add_argument("--listen", help="IP or off")
    wb.add_argument("--port", type=int)
    bn = sub.add_parser("bench"); bn.add_argument("--runs", type=int, default=10); bn.add_argument("--from", dest="src")
    ex = sub.add_parser("exec", help="NAME [--timeout S] [--stdin FILE] -- PROGRAM ARGS: run it in the guest (agent)")
    ex.add_argument("name"); ex.add_argument("--timeout", type=float, default=60)
    ex.add_argument("--stdin", help="file given to the program as its standard input")
    ex.add_argument("argv", nargs="*")  # after --: options of the program, not of exec
    ne = sub.add_parser("net"); nsub = ne.add_subparsers(dest="net_cmd", required=True)
    nsub.add_parser("create").add_argument("net"); nsub.add_parser("rm").add_argument("net")
    nsub.choices["create"].add_argument("--project")
    nsub.choices["create"].add_argument("--description", help="what it is for (one line)")
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
    if getattr(o, "description", None) is not None:  # refused before anything is made
        o.description = check_description(o.description)
    described = lambda kind, name: set_description(kind, name, o.description) if getattr(o, "description", None) else None

    opts = ({k: getattr(o, k) for k in ("os_name", "cpus", "memory", "disk", "width", "height", "layout", "offline", "product_key", "ui_language", "sandbox", "max_cpus", "max_memory", "project")}
            if o.cmd in ("up", "create") else {})
    try:
        if o.cmd == "up": return up(o.name, o.iso, o.no_view, o.plan, **opts)
        if o.cmd == "create":
            r = create(o.name, None if o.image else o.iso or default_image(os_name=o.os_name), image=o.image,
                       node=o.node, **opts)
            described("vm", o.name)
            return r
        if o.cmd == "start": return start(o.name, o.window)
        if o.cmd == "stop": return stop(o.name, o.force, o.timeout)
        if o.cmd == "resume" and not o.name: return resume_all(o.stay)
        if o.cmd in ("pause", "resume"): return pause(o.name, o.cmd == "resume")
        if o.cmd == "autostart": return print(json.dumps(autostart(None if o.state is None else o.state == "on"), indent=1))
        if o.cmd == "project":
            n_ok = {"ls": (0, 0), "show": (0, 1), "notes": (0, 2), "move": (2, 2)}[o.project_cmd]
            if not n_ok[0] <= len(o.args) <= n_ok[1]:
                raise SystemExit({"ls": "project ls", "show": "project show [PROJECT]", "notes": "project notes [PROJECT [TEXT]]",
                                  "move": "project move VM PROJECT"}[o.project_cmd])
            if o.project_cmd == "notes" and len(o.args) < 2:
                return print(project_status(*o.args)["notes"], end="")
            return print(json.dumps({"ls": lambda: projects(), "show": lambda: project_status(*o.args),
                                     "move": lambda: project_move(*o.args),
                                     "notes": lambda: notes_write(*o.args)}[o.project_cmd](), indent=1))
        if o.cmd == "ls": return ls()
        if o.cmd == "doctor": return doctor()
        if o.cmd == "help": return p.print_help()
        if o.cmd == "version": return print(f"terminal-vm {VERSION}")
        if o.cmd == "rm": return rm(o.name)
        if o.cmd == "reset": return reset(o.name)
        if o.cmd == "clone":
            r = clone(o.src, o.new)
            described("vm", o.new)
            return r
        if o.cmd == "describe":
            if o.desc_text is None and not o.clear:
                return print(json.dumps({o.desc_kind: o.desc_name, "description": description(o.desc_kind, o.desc_name)}))
            return print(json.dumps(describe(o.desc_kind, o.desc_name, "" if o.clear else o.desc_text)))
        if o.cmd == "snapshot": return snapshot(o.name, o.snap_cmd, o.tag)
        if o.cmd == "display": return display(o.name, o.width, o.height)
        if o.cmd == "layout": return set_layout(o.name, o.layout)
        if o.cmd == "sandbox": return set_sandbox(o.name, o.value)
        if o.cmd == "resources": return print(json.dumps(resources(o.name, o.cpus, o.memory, o.max_cpus, o.max_memory)))
        if o.cmd == "quota":
            change = {"cpus": o.cpus, "memory_gb": o.memory, "disk_gb": o.disk, "vms": o.vms}
            return print(json.dumps(quota(change if any(v is not None for v in change.values()) else None), indent=1))
        if o.cmd == "top": return print(json.dumps(top(), indent=1))
        if o.cmd == "daemon":
            r = daemon(o.daemon_cmd)
            return None if o.daemon_cmd == "run" else print(json.dumps(r, indent=1))
        if o.cmd == "supervise": return print(json.dumps(supervise(o.name, o.restart, o.idle_pause)))
        if o.cmd == "web": return print(json.dumps(web_cli(o.web_args, o.role, not o.no_open, o.project, o.listen,
                                                            o.port), indent=1))
        if o.cmd == "bench": return print(json.dumps(bench(max(1, o.runs), o.src), indent=1))
        if o.cmd == "exec":
            argv = o.argv  # argparse has dropped the -- itself
            if not argv:
                raise SystemExit("exec NAME -- PROGRAM [ARGS]")
            data = None
            if o.stdin:
                with open(o.stdin, "rb") as f:
                    data = f.read()
            # the guest's exit code is in the reply: a failing program is not a failing exec
            return print(json.dumps(guest_run(o.name, argv, max(1.0, min(o.timeout, 3600)), data)))
        if o.cmd == "events":
            for e in events(o.n, o.vm):
                print(json.dumps(e))
            return
        if o.cmd == "internet": return print(json.dumps(set_internet(o.name, o.value)))
        if o.cmd == "net":
            if o.net_cmd == "create":
                r = net_create(o.net, o.project)
                described("net", o.net)
                return print(json.dumps(r, indent=1))
            return print(json.dumps(
                net_create(o.net, o.project) if o.net_cmd == "create" else net_rm(o.net) if o.net_cmd == "rm" else
                net_ls() if o.net_cmd == "ls" else net_attach(o.name, o.net, o.net_cmd == "attach"), indent=1))
        if o.cmd == "image": return print(json.dumps({"image": default_image(o.plan, o.os_name)}))
        if o.cmd in ("push", "pull"): return transfer(o.name, o.cmd, o.src, o.dest)
        if o.cmd == "view": return view(o.name, read_only=o.read_only)
        if o.cmd == "show": return show(o.name, o.read_only)
        if o.cmd == "hide": return hide(o.name)
        if o.cmd == "mcp": return mcp(o.name, o.project)
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
        if o.tool == "screenshot" and o.no_wake:  # watching is not a use: as the dashboard's thumbnails
            qm, f = qmp(o.name), f"cli{os.getpid()}.png"
            try:
                out = screenshot(o.name, qm, f)
            finally:
                qm.close()
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(vm_dir(o.name), f))
        else:
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
