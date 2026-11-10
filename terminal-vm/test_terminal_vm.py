import contextlib, io, json, os, re, shutil, socket, struct, subprocess, sys, tempfile, threading, time, unittest, zlib
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import terminal_vm as tl

os.environ["TERMINAL_VM_DAEMON"] = "0"  # no supervision daemon spawned by the tests' starts

POSIX = os.name == "posix" and hasattr(socket, "AF_UNIX")


def tiny_png(w, h):
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\0" + b"\0\0\0" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def in_thread(f):
    """f() run in another thread: its result, or its exception raised here."""
    out = {}
    def run():
        try:
            out["r"] = f()
        except BaseException as e:
            out["e"] = e
    t = threading.Thread(target=run); t.start(); t.join()
    if "e" in out:
        raise out["e"]
    return out.get("r")


class FakeServer(threading.Thread):
    """Line-JSON unix socket server; `reply(msg)` returns the reply line(s) to write."""
    def __init__(self, path, greeting, reply):
        super().__init__(daemon=True)
        self.srv = socket.socket(socket.AF_UNIX); self.srv.bind(path); self.srv.listen()
        self.greeting, self.reply, self.log = greeting, reply, []
    def run(self):
        while True:
            c, _ = self.srv.accept()
            with c, c.makefile("rwb") as f:
                if self.greeting: f.write(self.greeting); f.flush()
                for line in f:
                    m = json.loads(line); self.log.append(m)
                    f.write(self.reply(m)); f.flush()


class Layouts(unittest.TestCase):
    def test_us(self):
        self.assertEqual(tl.char_keys("a"), [["a"]])
        self.assertEqual(tl.char_keys("A"), [["shift", "a"]])
        self.assertEqual(tl.char_keys("?"), [["shift", "slash"]])
        self.assertEqual(tl.char_keys(" "), [["spc"]])

    def test_fr(self):
        fr = lambda c: tl.char_keys(c, "fr")
        self.assertEqual(fr("a"), [["q"]])
        self.assertEqual(fr("M"), [["shift", "semicolon"]])
        self.assertEqual(fr("1"), [["shift", "1"]])
        self.assertEqual(fr("é"), [["2"]])
        self.assertEqual(fr("@"), [["alt_r", "0"]])
        self.assertEqual(fr("."), [["shift", "comma"]])
        self.assertEqual(fr("ê"), [["bracket_left"], ["e"]])
        self.assertEqual(fr("€"), [["alt_r", "e"]])
        # every printable ASCII character is typable without the Unicode fallback
        for c in map(chr, range(33, 127)):
            self.assertNotIn(["ctrl", "shift", "u"], fr(c), c)
            self.assertNotIn(["ctrl", "shift", "u"], tl.char_keys(c), c)

    def test_unicode_fallback(self):
        self.assertEqual(tl.char_keys("é"), [["ctrl", "shift", "u"], ["e"], ["9"], ["spc"]])  # U+00E9
        self.assertEqual(tl.char_keys("é", "fr")[0], ["2"])
        with self.assertRaises(ValueError): tl.char_keys("\x07")

    def test_chords(self):
        self.assertEqual(tl.chord_keys("Ctrl+Alt+Delete"), ["ctrl", "alt", "delete"])
        self.assertEqual(tl.chord_keys("cmd+space"), ["meta_l", "spc"])
        self.assertEqual(tl.chord_keys("ctrl+a", "fr"), ["ctrl", "q"])
        self.assertEqual(tl.chord_keys("ctrl+w", "fr"), ["ctrl", "z"])
        with self.assertRaises(ValueError): tl.chord_keys("ctrl+")


class Viewer(unittest.TestCase):
    def test_parse_input(self):
        P = tl.parse_input
        self.assertEqual(P("hé!"), [("text", "hé!")])
        self.assertEqual(P("\r\x7f\t"), [("key", "ret"), ("key", "backspace"), ("key", "tab")])
        self.assertEqual(P("\x01\x1d\x0f"), [("key", "ctrl+a"), ("quit",), ("show",)])
        self.assertEqual(P("\x1b[A\x1b[1;5C\x1b[3~\x1bOP\x1b[15~"),
                         [("key", "up"), ("key", "ctrl+right"), ("key", "delete"), ("key", "f1"), ("key", "f5")])
        self.assertEqual(P("\x1bx\x1b"), [("key", "alt+x"), ("key", "esc")])
        self.assertEqual(P("\x1b[<0;120;45M\x1b[<0;120;45m\x1b[<65;3;4M"),
                         [("mouse", 0, 120, 45, True), ("mouse", 0, 120, 45, False), ("mouse", 65, 3, 4, True)])
        self.assertEqual(P("\x1b[6;20;10ta"), [("cell", 20, 10), ("text", "a")])   # reply to CSI 16 t

    def test_sixel(self):
        """Decode the encoder's output with an independent reader: every pixel shows its own colour (5 bits/channel)."""
        w, h = 9, 8
        colours = [(255, 0, 0), (0, 136, 255), (17, 34, 51), (240, 240, 240), (0, 0, 0)]
        rgb = bytes(v for i in range(w * h) for v in colours[i * 7 % 5])
        key = lambda r, g, b: (r >> 3) << 10 | (g >> 3) << 5 | b >> 3
        pal = tl.make_palette(rgb, w, h)
        self.assertEqual(sorted(pal[0]), sorted(key(*c) for c in colours))
        for W, H in ((9, 8), (14, 13), (23, 20)):  # same and larger: nearest pixel, colours exact
            s = tl.sixel(tl.sixel_rows(rgb, w, h, W, H, pal), pal[0]).decode()
            self.assertTrue(s.startswith("\x1bPq") and s.endswith("\x1b\\"))
            self.assertIn(f'"1;1;{W};{H}', s)
            regs = {int(i): (int(r), int(g), int(b)) for i, r, g, b in re.findall(r"#(\d+);2;(\d+);(\d+);(\d+)", s)}
            self.assertEqual(regs[pal[0].index(key(0, 136, 255))], (0, 55, 100))   # percent of 5-bit levels
            got = [[None] * W for _ in range(H)]
            body = re.sub(r"#\d+;2;\d+;\d+;\d+", "", s[s.index("#"):-2])
            for band_no, band in enumerate(body.split("-")[:-1]):
                for layer in band.split("$"):
                    c, data = re.match(r"#(\d+)(.*)", layer).groups()
                    data = re.sub(r"!(\d+)(.)", lambda m: m[2] * int(m[1]), data)
                    self.assertEqual(len(data), W)
                    for x, ch in enumerate(data):
                        for k in range(6):
                            if (ord(ch) - 63) >> k & 1:
                                self.assertIsNone(got[band_no * 6 + k][x])  # one colour per pixel
                                got[band_no * 6 + k][x] = pal[0][int(c)]
            src = lambda X, Y: (Y * h // H * w + X * w // W) * 3   # nearest source pixel
            want = [[key(*rgb[src(X, Y):src(X, Y) + 3]) for X in range(W)] for Y in range(H)]
            self.assertEqual(got, want)

    def test_shrink_keeps_thin_lines(self):
        """Shrinking blends neighbours: a 1-pixel line that nearest-neighbour would skip still shows."""
        w, h = 12, 12
        white, black = bytes([255, 255, 255]), bytes([0, 0, 0])
        rgb = b"".join(black if x == 7 else white for y in range(h) for x in range(w))   # vertical line at x=7
        pal = tl.make_palette(rgb, w, h)
        self.assertNotIn(7, [x * w // 9 for x in range(9)])                  # 12 -> 9 skips column 7
        rows = tl.sixel_rows(rgb, w, h, 9, 9, pal)
        white_reg = pal[1][(31 << 10) | (31 << 5) | 31]
        self.assertTrue(all(any(c != white_reg for c in r) for r in rows))   # every row still shows the line
        self.assertEqual({r[0] for r in rows}, {white_reg})                  # away from it, plain white

    def test_palette_nearest(self):
        """Colours outside the palette take the nearest register; the most frequent colours are kept."""
        rgb = bytes([255, 0, 0]) * 50 + bytes([0, 0, 255]) * 40 + bytes([240, 16, 16]) * 10   # 10 x 10
        keys, lut, cells = tl.make_palette(rgb, 10, 10, size=2)
        self.assertEqual(keys, [31 << 10, 31])
        row = tl.sixel_rows(bytes([224, 32, 0]) * 10, 10, 1, 10, 1, (keys, lut, cells))[0]  # dark red: unseen
        self.assertEqual(set(row), {0})

    def test_changed(self):
        old = [bytes(12) for _ in range(5)]
        self.assertEqual(tl.changed(old, list(old)), [])
        new = list(old)
        new[1] = bytes(3) + b"\x07" + bytes(8)            # one pixel at x=3
        new[3] = bytes(9) + b"\x01\x00\x02"               # x=9 and x=11
        self.assertEqual(tl.changed(old, new), [(3, 1, 12, 4)])                  # rows 1 and 3: one run
        self.assertEqual(tl.changed(old, new, gap=1), [(3, 1, 4, 2), (9, 3, 12, 4)])  # apart: two boxes
        new = [b"\x01" + bytes(11)] + old[1:]             # first byte only
        self.assertEqual(tl.changed(old, new), [(0, 0, 1, 1)])

    def test_fit(self):
        self.assertEqual(tl.fit(1280, 800, 100, 41, 10, 20), (100, 31))   # width-bound
        self.assertEqual(tl.fit(1280, 800, 300, 21, 10, 20), (64, 20))    # height-bound

    def test_abs_scaling(self):
        self.assertEqual(tl.to_abs(0, 640), 0)
        self.assertEqual(tl.to_abs(639, 640), 0x7FFF)
        self.assertEqual(tl.to_abs(9999, 640), 0x7FFF)


@unittest.skipUnless(POSIX, "needs unix sockets")
class Qmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); tl.STORE = self.tmp
        d = os.path.join(self.tmp, "t"); os.makedirs(d)
        with open(os.path.join(d, "machine.json"), "w") as f:
            json.dump({"layout": "fr", "cpus": 1, "memory_mb": 1024}, f)
        self.status, self.hmp = "running", ""
        def reply(m):
            if m["execute"] == "screendump":
                with open(m["arguments"]["filename"], "wb") as f:  # QEMU: PNG if asked, else raw PPM
                    f.write(tiny_png(640, 480) if m["arguments"].get("format") == "png"
                            else b"P6\n640 480\n255\n" + bytes(640 * 480 * 3))
            if m["execute"] == "human-monitor-command":
                return json.dumps({"return": self.hmp}).encode() + b"\n"
            return b'{"event": "X"}\n' + json.dumps({"return": {"status": self.status}}).encode() + b"\n"
        self.q = FakeServer(os.path.join(d, "qmp.sock"), b'{"QMP": {}}\n', reply); self.q.start()

    def test_watch_marks_a_guest_shutdown_as_wanted_off(self):
        tl.want("t", "running")
        ev = b'{"event": "SHUTDOWN", "data": {"guest": true, "reason": "guest-shutdown"}}\n'
        FakeServer(os.path.join(self.tmp, "t", "qmp-watch.sock"), b'{"QMP": {}}\n',
                   lambda m: b'{"return": {}}\n' + ev).start()
        tl.watch("t")                                                         # returns at the guest's shutdown
        self.assertEqual(tl.wanted("t"), "stopped")                           # resume will leave it off

    def test_click_type_and_layout(self):
        tl.act("t", "click", {"x": 639, "y": 0, "button": "right"})
        events = [m for m in self.q.log if m["execute"] == "input-send-event"]
        self.assertEqual(events[0]["arguments"]["events"][0]["data"]["value"], 0x7FFF)
        self.assertEqual(events[1]["arguments"]["events"][0]["data"], {"down": True, "button": "right"})
        self.q.log.clear()
        tl.act("t", "type", {"text": "Aé"})  # fr layout
        self.assertEqual([[k["data"] for k in m["arguments"]["keys"]] for m in self.q.log if m["execute"] == "send-key"],
                         [["shift", "q"], ["2"]])

    def test_layout_override_and_command(self):
        tl.act("t", "type", {"text": "a", "layout": "us"})      # VM is fr: this call types QWERTY
        self.assertEqual([m["arguments"]["keys"][0]["data"] for m in self.q.log if m["execute"] == "send-key"], ["a"])
        with self.assertRaises(ValueError):
            tl.act("t", "type", {"text": "a", "layout": "de"})
        with contextlib.redirect_stdout(io.StringIO()):
            tl.set_layout("t", "us")
        self.assertEqual(tl.load("t")["layout"], "us")

    def test_scroll_batches_press_and_release(self):
        tl.act("t", "scroll", {"dy": -200})
        wheels = [m["arguments"]["events"] for m in self.q.log if m["execute"] == "input-send-event"]
        self.assertEqual(len(wheels), 2)
        self.assertEqual([e["data"]["down"] for e in wheels[0]], [True, False])
        self.assertEqual(wheels[0][0]["data"]["button"], "wheel-down")

    def test_paused_rejects_input(self):
        self.status = "paused"
        with self.assertRaisesRegex(RuntimeError, "paused"):
            tl.act("t", "click", {"x": 1, "y": 1})
        self.assertEqual(tl.state("t"), "paused")

    def test_guest_users(self):
        self.assertIsNone(tl.guest_users("t"))  # no agent socket
        files = {"/run/systemd/seats/seat0": "# private\nACTIVE=3\nACTIVE_UID=1000\n",
                 "/run/systemd/sessions/3": "USER=alice\nCLASS=user\nTYPE=x11\n"}
        handles = {}
        def qga(m):
            cmd, a = m["execute"], m.get("arguments", {})
            if cmd == "guest-sync": r = a["id"]
            elif cmd == "guest-file-open": handles[len(handles) + 1] = a["path"]; r = len(handles)
            elif cmd == "guest-file-read":
                import base64; r = {"buf-b64": base64.b64encode(files[handles[a["handle"]]].encode()).decode(), "eof": True}
            else: r = {}
            return b"\xff" + json.dumps({"return": r}).encode() + b"\n"
        FakeServer(os.path.join(self.tmp, "t", "qga.sock"), None, qga).start()
        self.assertEqual(tl.guest_users("t"), ["alice"])
        self.assertEqual(tl.act("t", "status", {})["users"], ["alice"])
        files["/run/systemd/sessions/3"] = "USER=lightdm\nCLASS=greeter\n"   # login screen
        self.assertEqual(tl.guest_users("t"), [])

    def test_wait_settles_on_identical_screens(self):
        out = tl.act("t", "wait", {"stable": 0.3, "timeout": 5})
        self.assertTrue(out["stable"])

    def test_snapshot_running_uses_hmp(self):
        tl.snapshot("t", "save", "clean")
        self.assertEqual([m["arguments"]["command-line"] for m in self.q.log if m["execute"] == "human-monitor-command"],
                         ["savevm clean"])
        self.hmp = ("List of snapshots present on all disks:\nID        TAG               VM SIZE                DATE     VM CLOCK\n"
                    "--        clean             1.2 GiB 2026-11-06 10:00:00 00:01:02.003\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tl.snapshot("t", "ls")
        self.assertEqual(json.loads(buf.getvalue())["snapshots"], ["clean"])
        self.hmp = "Error: Snapshot 'nope' does not exist in one or more devices"
        with self.assertRaisesRegex(SystemExit, "does not exist"):
            tl.snapshot("t", "load", "nope")
        with self.assertRaises(SystemExit):
            tl.snapshot("t", "save", "bad tag")  # rejected before reaching QEMU
        self.hmp = ""
        with self.assertRaisesRegex(SystemExit, "no snapshot"):
            tl.snapshot("t", "rm", "nope")       # delvm is silent about a missing tag

    def test_running_vm_refuses_destructive_commands(self):
        for f in (lambda: tl.rm("t"), lambda: tl.reset("t"), lambda: tl.display("t", 1024, 768)):
            with self.assertRaisesRegex(SystemExit, "stop it first"):
                f()
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "t")))

    def test_push_pull(self):
        guest, handles = {}, {}
        def qga(m):
            cmd, a = m["execute"], m.get("arguments", {})
            if cmd == "guest-sync": r = a["id"]
            elif cmd == "guest-file-open":
                handles[len(handles) + 1] = [a["path"], 0]; r = len(handles)
                if "w" in a.get("mode", "r"): guest[a["path"]] = b""
            elif cmd == "guest-file-write":
                import base64; guest[handles[a["handle"]][0]] += base64.b64decode(a["buf-b64"]); r = {}
            elif cmd == "guest-file-read":  # one byte per read: exercises the read loop
                import base64; h = handles[a["handle"]]; chunk = guest[h[0]][h[1]:h[1] + 1]; h[1] += 1
                r = {"buf-b64": base64.b64encode(chunk).decode(), "count": len(chunk), "eof": h[1] >= len(guest[h[0]])}
            else: r = {}
            return json.dumps({"return": r}).encode() + b"\n"
        FakeServer(os.path.join(self.tmp, "t", "qga.sock"), None, qga).start()
        src, back = os.path.join(self.tmp, "a.bin"), os.path.join(self.tmp, "b.bin")
        with open(src, "wb") as f: f.write(b"\x00\xffhi")
        tl.transfer("t", "push", src, "/tmp/a.bin")
        self.assertEqual(guest["/tmp/a.bin"], b"\x00\xffhi")
        tl.transfer("t", "pull", "/tmp/a.bin", back)
        with open(back, "rb") as f: self.assertEqual(f.read(), b"\x00\xffhi")


class WindowsGuests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); tl.STORE = self.tmp

    def args(self, os_name, vnc_port=None):
        m = {"os": os_name, "iso": "/x.iso", "memory_mb": 2048, "cpus": 2}
        os.makedirs(os.path.join(self.tmp, "v", "answers"), exist_ok=True)
        return " ".join(tl.qemu_args("v", m, False, vnc_port))

    def test_hardware_profiles(self):
        xp = self.args("winxp")
        for flag in ("-machine pc", "if=ide", "-device cirrus-vga ", "-usb ", "-cpu qemu64", "model=rtl8139",
                     "if=floppy,index=0,driver=vvfat"):
            self.assertIn(flag, xp)
        w11 = self.args("win11")
        for flag in ("-machine q35", "if=ide", "VGA,xres=1280,yres=800", "qemu-xhci", "model=e1000e", "usb-storage,drive=answers,removable=on"):
            self.assertIn(flag, w11)
        self.assertIn("usb-ehci", self.args("win7"))      # Windows 7 has no xHCI driver
        lin = self.args("linux")
        self.assertIn("if=virtio", lin); self.assertNotIn("answers", lin)

    def test_hyperv_enlightenments_on_kvm_only(self):
        old = tl.accelerators
        try:
            tl.accelerators = lambda: (["-accel", "kvm"], "host")
            self.assertIn("-cpu host,hv_relaxed,", self.args("win2025"))
            self.assertNotIn("hv_", self.args("linux"))
            self.assertIn("-cpu qemu64 ", self.args("winxp"))   # XP keeps its generic CPU
            tl.accelerators = lambda: (["-accel", "whpx"], "max")
            self.assertNotIn("hv_", self.args("win2025"))       # KVM-only flags
        finally:
            tl.accelerators = old

    def test_install_cpus_until_installed(self):
        m = {"os": "win2025", "iso": "/x.iso", "memory_mb": 2048, "cpus": 2, "install_cpus": 6}
        os.makedirs(os.path.join(self.tmp, "v", "answers"), exist_ok=True)
        self.assertIn("-smp 6 ", " ".join(tl.qemu_args("v", m, False)))
        m["installed"] = True
        self.assertIn("-smp 2 ", " ".join(tl.qemu_args("v", m, False)))

    def test_free_ports_distinct(self):
        self.assertEqual(len(set(tl.free_ports(4))), 4)

    def test_windows_host_endpoints(self):
        a = self.args("win10", 40004)
        for flag in ("-qmp unix:", "qmp.sock,server=on", "qmp-view.sock", "socket,path=", "qga.sock",
                     "-vnc 127.0.0.1:34104,password=on"):     # only VNC is TCP, and it needs a password
            self.assertIn(flag, a)
        self.assertNotIn("tcp:", a)

    def test_autounattend(self):
        from xml.etree import ElementTree as ET
        ns = "{urn:schemas-microsoft-com:unattend}"
        for os_name in ("win7", "win8", "win10", "win11", "win2025"):
            p = tl.PROFILES[os_name]
            xml = tl.autounattend(p, "fr", "user", "p<&>w-Aa1", p["key"] if p["key"] != "required" else "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE")
            root = ET.fromstring(xml)                              # well-formed despite <&> in the password
            self.assertEqual(root.tag, ns + "unattend")
            self.assertIn("040c:0000040c", xml)
            self.assertIn("<UILanguage>en-US</UILanguage>", xml)   # the media's language, not the keyboard's
            self.assertNotIn("<Letter>", xml)
            self.assertEqual("BypassTPMCheck" in xml, os_name == "win11")
            self.assertEqual("/IMAGE/INDEX" in xml, os_name == "win2025")
            self.assertEqual("HideOnlineAccountScreens" in xml, os_name != "win7")
            self.assertIn("EnableHexNumpad", xml)
            # set in the first session, then a logoff so the agent's session (forced auto-logon again) has it
            self.assertLess(xml.index("EnableHexNumpad"), xml.index("shutdown /l"))
            self.assertLess(xml.index("ForceAutoLogon"), xml.index("shutdown /l"))  # else stuck at Ctrl+Alt+Delete
            self.assertIn("<FirstLogonCommands>", xml)
            self.assertIn("<AdministratorPassword><Value>p&lt;&amp;&gt;w-Aa1</Value>", xml)  # Server OOBE stops without it
            spec = root.find(f"{ns}settings[@pass='specialize']")  # default profile, so the first session has it
            spec_xml = ET.tostring(spec, encoding="unicode")
            for needle in ("monitor-timeout-ac 0", "CONSOLELOCK 0"):
                self.assertIn(needle, spec_xml)   # never locked or blanked on idle
            self.assertLess(xml.index("DisableLockWorkstation"), xml.index("shutdown /l"))  # no Win+L, so stop works
        self.assertIn("W269N-WFGWX-YVC9B-4J6C9-T83GX", tl.autounattend(tl.PROFILES["win10"], "us", "u", "p", tl.PROFILES["win10"]["key"]))
        self.assertNotIn("<ProductKey>", tl.autounattend(tl.PROFILES["win2025"], "us", "u", "p", None))

    def test_winnt_sif(self):
        sif = tl.winnt_sif("us", "secret-Aa1", "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE")
        for line in ("UnattendMode=FullUnattended", 'AdminPassword="secret-Aa1"', "ProductKey=AAAAA-BBBBB-CCCCC-DDDDD-EEEEE",
                     "AutoLogon=Yes", "EnableHexNumpad"):
            self.assertIn(line, sif)
        self.assertIn("\r\n", sif)

    def test_hex_numpad(self):
        ev = tl.hex_numpad_events("ñ", "fr")  # U+00F1: Alt, numpad +, f, 1
        keys = [(e["data"]["key"]["data"], e["data"]["down"]) for e in ev]
        self.assertEqual(keys, [("alt", True), ("kp_add", True), ("kp_add", False), ("f", True), ("f", False),
                                ("kp_1", True), ("kp_1", False), ("alt", False)])

    def test_create_windows(self):
        calls = []
        old = (tl.subprocess.run, tl.qemu_bin)
        tl.subprocess.run, tl.qemu_bin = (lambda *a, **k: calls.append(a)), (lambda t: t)
        iso = os.path.join(self.tmp, "w.iso"); open(iso, "w").close()
        tools = os.path.join(self.tmp, "tools.exe")
        with open(tools, "wb") as f: f.write(b"MZ fake")
        old_tools, tl.guest_tools = tl.guest_tools, lambda: tools   # no download in tests
        with open(os.path.join(self.tmp, "quota.json"), "w") as f:
            json.dump({"disk_gb": 1000, "memory_gb": 64, "cpus": 64}, f)   # independent of this host's free space
        try:
            with self.assertRaises(SystemExit):  # Windows 8 and XP need the user's key
                tl.create("w8", iso, os_name="win8")
            tl.create("w11", iso, os_name="win11", layout="fr")
        finally:
            tl.subprocess.run, tl.qemu_bin = old
            tl.guest_tools = old_tools
        with open(os.path.join(self.tmp, "w11", "machine.json")) as f:
            m = json.load(f)
        self.assertEqual((m["os"], m["memory_mb"], m["user"]), ("win11", 4096, "user"))
        self.assertTrue(m["password"].endswith("-Aa1") and len(m["password"]) == 12)
        self.assertIn("64G", next(c[0] for c in calls if "create" in c[0]))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "w11", "answers", "autounattend.xml")))
        with open(os.path.join(self.tmp, "w11", "answers", "virtio-win-guest-tools.exe"), "rb") as f:
            self.assertEqual(f.read(), b"MZ fake")   # the guest agent's installer rides on the answers drive
        with open(os.path.join(self.tmp, "w11", "answers", "autounattend.xml"), encoding="utf-8") as f:
            xml = f.read()
        self.assertLess(xml.index("virtio-win-guest-tools.exe /install /quiet"), xml.index("shutdown /l"))
        self.assertIn("<TimeZone>UTC</TimeZone>", xml)  # tzutil faked: no answer, so UTC
        self.assertTrue(m["rtc_local"])  # installs from these answers: local-time RTC
        self.assertIn("-rtc base=localtime", " ".join(tl.qemu_args("w11", m, False)))
        self.assertNotIn("-rtc", " ".join(tl.qemu_args("w11", dict(m, rtc_local=False), False)))  # older VMs keep UTC
        self.assertIn("ServerManager /v DoNotOpenAtLogon", xml)

    def test_reset_rewrites_stale_answers(self):
        """A VM made by an older version reinstalls with today's answers, same account, language and key."""
        def run(cmd, *a, **k):
            if "create" in cmd:
                open(cmd[-2], "w").close()
            return type("R", (), {"stdout": '{"virtual-size": 1}', "returncode": 0})()
        old = (tl.subprocess.run, tl.qemu_bin, tl.guest_tools, tl.state)
        tools = os.path.join(self.tmp, "tools.exe")
        with open(tools, "wb") as f: f.write(b"MZ fake")
        tl.subprocess.run, tl.qemu_bin, tl.guest_tools, tl.state = run, (lambda t: t), (lambda: tools), (lambda n: "stopped")
        iso = os.path.join(self.tmp, "w.iso"); open(iso, "w").close()
        with open(os.path.join(self.tmp, "quota.json"), "w") as f:
            json.dump({"disk_gb": 1000, "memory_gb": 64, "cpus": 64}, f)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                tl.create("w", iso, os_name="win11", layout="fr", ui_language="fr-FR", product_key="AAAAA-BBBBB-CCCCC-DDDDD-EEEEE")
                ans = os.path.join(self.tmp, "w", "answers")
                os.remove(os.path.join(ans, "virtio-win-guest-tools.exe"))
                with open(os.path.join(ans, "autounattend.xml"), "w") as f:  # as written before the guest tools existed
                    f.write("<UILanguage>fr-FR</UILanguage><ProductKey><Key>AAAAA-BBBBB-CCCCC-DDDDD-EEEEE</Key>")
                pw = tl.load("w")["password"]
                tl.reset("w")
        finally:
            tl.subprocess.run, tl.qemu_bin, tl.guest_tools, tl.state = old
        with open(os.path.join(ans, "autounattend.xml"), encoding="utf-8") as f:
            xml = f.read()
        self.assertIn("virtio-win-guest-tools.exe /install", xml)
        self.assertIn("<UILanguage>fr-FR</UILanguage>", xml)
        self.assertIn("AAAAA-BBBBB-CCCCC-DDDDD-EEEEE", xml)
        self.assertIn(pw, xml)
        self.assertTrue(os.path.isfile(os.path.join(ans, "virtio-win-guest-tools.exe")))

    def test_download_checks(self):
        """Downloads are kept only over HTTPS and with the pinned SHA-256."""
        class Resp(io.BytesIO):
            def __init__(self, body, url): super().__init__(body); self.url, self.headers = url, {}
            def geturl(self): return self.url
            def __enter__(self): return self
            def __exit__(self, *a): pass
        old = tl.urllib.request.urlopen
        dest = os.path.join(self.tmp, "img", "f.bin")
        good = __import__("hashlib").sha256(b"data").hexdigest()
        try:
            tl.urllib.request.urlopen = lambda url, timeout: Resp(b"data", "https://x/f.bin")
            with self.assertRaises(SystemExit):
                tl.download("https://x/f.bin", dest, "0" * 64)
            self.assertFalse(os.path.exists(dest) or os.path.exists(dest + ".part"))
            tl.urllib.request.urlopen = lambda url, timeout: Resp(b"data", "http://x/f.bin")
            with self.assertRaises(SystemExit):
                tl.download("https://x/f.bin", dest, good)
            self.assertFalse(os.path.exists(dest) or os.path.exists(dest + ".part"))
            tl.urllib.request.urlopen = lambda url, timeout: Resp(b"data", "https://x/f.bin")
            self.assertEqual(tl.download("https://x/f.bin", dest, good), dest)
        finally:
            tl.urllib.request.urlopen = old


class MacHandoff(unittest.TestCase):
    """terminal-vm hands macOS guests to terminal-mac on a Mac, unchanged, and refuses them elsewhere."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (tl.MAC, tl.MAC_HOME, tl.STORE, tl.mac_bin, tl.subprocess.run)
        tl.STORE, tl.MAC_HOME = os.path.join(self.tmp, "vm"), os.path.join(self.tmp, "mac")
        os.makedirs(os.path.join(tl.MAC_HOME, "machines", "mymac"))
        os.makedirs(os.path.join(tl.STORE, "lin"))
        with open(os.path.join(tl.STORE, "lin", "machine.json"), "w") as f:
            json.dump({"cpus": 2, "memory_mb": 2048}, f)

    def tearDown(self):
        tl.MAC, tl.MAC_HOME, tl.STORE, tl.mac_bin, tl.subprocess.run = self.old

    def test_on_a_mac(self):
        tl.MAC, H = True, tl.macos_handoff
        self.assertEqual(H(["create", "m", "--os", "macos", "--ipsw", "x.ipsw"]), ["create", "m", "--ipsw", "x.ipsw"])
        self.assertEqual(H(["up"]), ["up"])                                     # a Mac's own guest by default
        self.assertIsNone(H(["up", "w", "--os", "win11"]))                     # QEMU guests stay here
        self.assertEqual(H(["action", "mymac", "click", "10", "20"]), ["action", "mymac", "click", "10", "20"])
        self.assertEqual(H(["mcp", "mymac"]), ["mcp", "mymac"])
        self.assertIsNone(H(["start", "lin"]))                                 # one of ours
        self.assertIsNone(H(["start", "nosuch"]))                              # unknown: our own error message
        self.assertIsNone(H(["ls"]))

    def test_elsewhere(self):
        tl.MAC = False
        with self.assertRaises(SystemExit) as e:
            tl.macos_handoff(["create", "m", "--os", "macos"])
        self.assertIn("Apple", str(e.exception))
        self.assertIsNone(tl.macos_handoff(["start", "mymac"]))
        self.assertIsNone(tl.macos_handoff(["up"]))

    def test_ls_merges_terminal_mac(self):
        tl.MAC, tl.mac_bin = True, lambda: "terminal-mac"
        reply = json.dumps([{"name": "mymac", "cpu": 4, "memoryGiB": 8, "width": 1280, "height": 800}])
        tl.subprocess.run = lambda args, **k: tl.subprocess.CompletedProcess(args, 0, reply, "")
        self.assertEqual(tl.mac_machines(), [{"name": "mymac", "os": "macos", "engine": "terminal-mac", "cpus": 4,
                                              "memory_gb": 8, "display": "1280x800"}])
        tl.subprocess.run = lambda args, **k: tl.subprocess.CompletedProcess(args, 1, "", "boom")
        self.assertEqual(tl.mac_machines(), [])


class NoWake(unittest.TestCase):
    """action VM screenshot --no-wake (terminal-ct's thumbnails): looking resumes no paused VM."""
    def test_screenshot_without_waking(self):
        tl.STORE = tempfile.mkdtemp(); os.makedirs(os.path.join(tl.STORE, "v"))
        closed, out = [], os.path.join(tl.STORE, "shot.png")

        class Q:
            def close(self):
                closed.append(1)

        def act(*a, **k):
            raise AssertionError("act() wakes the VM")
        with mock.patch.object(tl, "qmp", lambda n: Q()), mock.patch.object(tl, "act", act), \
                mock.patch.object(tl, "screenshot", lambda n, q=None, file="": (b"PNG", (4, 3))), \
                contextlib.redirect_stdout(io.StringIO()) as o:
            tl.main(["action", "v", "screenshot", "--no-wake", "--output", out])
        self.assertEqual(json.loads(o.getvalue())["width"], 4)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"PNG")
        self.assertEqual(closed, [1])                                           # QMP takes one client: closed


class StoppedVm(unittest.TestCase):
    """rm, reset, display and offline snapshots: no QEMU running, qemu-img faked."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); tl.STORE = self.tmp
        self.d = os.path.join(self.tmp, "v"); os.makedirs(self.d)
        with open(os.path.join(self.d, "machine.json"), "w") as f:
            json.dump({"cpus": 1, "memory_mb": 1024, "width": 1280, "height": 800}, f)
        with open(os.path.join(self.d, "disk.qcow2"), "w") as f: f.write("old")
        with open(os.path.join(self.tmp, "quota.json"), "w") as f:
            json.dump({"disk_gb": 1000, "memory_gb": 64, "cpus": 64}, f)   # independent of this host
        self.calls, self.old = [], (tl.subprocess.run, tl.qemu_bin)
        self.snaps = ["clean"]
        def run(argv, **k):
            self.calls.append(argv)
            if argv[1] == "create":  # the image created, holding its backing file's path
                with open([a for a in argv if a.endswith((".qcow2", ".new"))][-1], "w") as f:
                    f.write(argv[argv.index("-b") + 1] if "-b" in argv else "")
            if argv[1] == "info":
                with open(argv[-1]) as f:
                    b = f.read()
                out = json.dumps({"virtual-size": 68719476736, **({"backing-filename": b} if b.endswith(".qcow2") else {})})
            else:
                out = "ID TAG VM SIZE DATE\n" + "".join(f"1 {t} 0 B 2026-11-06 10:00:00\n" for t in self.snaps) \
                    if argv[1:3] == ["snapshot", "-l"] else "[]" if argv[1] == "map" else ""  # map: no own data
            return tl.subprocess.CompletedProcess(argv, 0, out, "")
        tl.subprocess.run, tl.qemu_bin = run, (lambda t: t)

    def tearDown(self):
        tl.subprocess.run, tl.qemu_bin = self.old

    def quiet(self, f, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f(*a)
        return json.loads(buf.getvalue())

    def vm(self, name, **m):
        os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
        tl.save(name, {"cpus": 1, "memory_mb": 1024, "width": 1280, "height": 800, "disk_gb": 4, **m})

    def test_project_scope_is_closed(self):
        yes = lambda m, c: None
        self.vm("a", project="alpha", created_by="agent"); self.vm("w", project="alpha/web", created_by="agent")
        self.vm("b", project="beta", created_by="agent")
        listed = sorted(v["name"] for v in tl.agent_call("vm_list", {}, yes, project="alpha"))
        self.assertEqual(listed, ["a", "w"])                                  # its sub-project too, not beta
        for tool, a in (("vm_start", {"vm": "b"}), ("vm_resources", {"vm": "b"}), ("vm_internet", {"vm": "b"})):
            with self.assertRaisesRegex(SystemExit, "no VM named 'b'"):
                tl.agent_call(tool, a, yes, project="alpha")                  # even approving would not help
        with self.assertRaisesRegex(SystemExit, "not this agent's project"):
            tl.agent_call("notes_write", {"text": "x", "project": "beta"}, yes, project="alpha")
        with self.assertRaisesRegex(SystemExit, "is not available: choose another"):  # no project, no detail
            tl.agent_call("vm_clone", {"vm": "a", "new": "b"}, yes, project="alpha")
        tl.net_create("bnet", "beta")
        with self.assertRaisesRegex(SystemExit, "no network named 'bnet'"):
            tl.agent_call("net_attach", {"vm": "a", "net": "bnet"}, yes, project="alpha")
        self.assertEqual(tl.agent_call("net_list", {}, yes, project="alpha"), [])
        q = tl.scoped_quota("alpha")
        self.assertEqual(q["cpus"]["used_by_project"], 0); self.assertNotIn("used", q["cpus"])
        tl.agent_call("notes_write", {"text": "web: nginx left"}, yes, project="alpha/web")
        st = tl.project_status("alpha")
        self.assertEqual((sorted(v["name"] for v in st["vms"]), st["sub_projects"]), (["a", "w"], ["alpha/web"]))
        self.assertIn("nginx left", tl.project_status("alpha/web")["notes"])
        self.assertEqual(tl.project_move("b", "alpha"), {"vm": "b", "project": "alpha"})

    def test_mcp_project_status_and_robust_log(self):
        self.vm("a", project="site", created_by="agent")
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "notes_write", "arguments": {"text": "t", "project": "../x"}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "project_status", "arguments": {}}},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "screenshot", "arguments": {"vm": "v"}}},
                 {"jsonrpc": "2.0", "id": 5, "method": "ping"}]
        old = sys.stdin
        sys.stdin = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tl.mcp("a", "site")
        finally:
            sys.stdin = old
        msgs = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([m["id"] for m in msgs], [1, 2, 3, 4, 5])               # still serving
        self.assertIn("Call infra first", msgs[0]["result"]["instructions"])
        self.assertTrue(msgs[1]["result"]["isError"])
        self.assertEqual([v["name"] for v in json.loads(msgs[2]["result"]["content"][0]["text"])["vms"]], ["a"])
        self.assertIn("no VM named 'v'", msgs[3]["result"]["content"][0]["text"])  # v is default's

    def test_project_server_without_its_vm_and_exec_approval(self):
        self.vm("a", project="site")                                          # the user's VM in the project
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "screenshot", "arguments": {}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "exec", "arguments": {"vm": "a", "command": "id"}}}]
        old = sys.stdin
        sys.stdin = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tl.mcp("v", "site")                                           # v is default's: served without it
        finally:
            sys.stdin = old
        msgs = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertIn("name the VM", msgs[1]["result"]["content"][0]["text"])
        self.assertIn("belongs to the user", msgs[2]["result"]["content"][0]["text"])  # not its own: off-limits
        tl.save("a", dict(tl.load("a"), sandbox=True))
        with self.assertRaisesRegex(SystemExit, "no guest agent"):
            tl.guest_shell("a", "id")
        net = tl.net_create("n", "site")
        self.vm("q", project="other"); m = tl.load("q"); m["networks"] = {"n": {"mac": "x", "ip": "10.77.1.10"}}; tl.save("q", m)
        self.assertEqual(tl.agent_call("net_list", {}, None, project="site")[0]["vms"], [])  # q is another project's

    def test_want_and_resume(self):
        self.quiet(tl.stop, "v")
        self.assertEqual(tl.wanted("v"), "stopped")
        self.vm("up"); tl.want("up", "running")
        started = []
        with mock.patch.object(tl, "start", lambda n, window=False: (started.append(n), print(json.dumps({"started": n})))):
            out = self.quiet(tl.resume_all)
        self.assertEqual((started, [r["name"] for r in out["resumed"]]), (["up"], ["up"]))

    def test_autostart_commands(self):
        with mock.patch.object(tl, "in_wsl", lambda: True), mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Debian"}):
            out = tl.autostart(True)
        self.assertTrue(out["runs"].startswith("conhost.exe --headless wsl.exe -d Debian --exec env "))
        self.assertTrue(out["runs"].endswith(" resume --stay"))
        self.assertEqual(self.calls[-1][:5], ["schtasks.exe", "/Create", "/TN", "terminal-vm-resume", "/SC"])
        with self.assertRaisesRegex(RuntimeError, "terminal-vm autostart on"):
            tl.agent_call("autostart", {"on": True}, lambda m, c: (_ for _ in ()).throw(RuntimeError(c)))
        tl.save("v", dict(tl.load("v"), sandbox=True))
        with self.assertRaisesRegex(SystemExit, "sandbox: no guest agent"):
            tl.guest_shell("v", "true")

    def test_reset_keeps_size_and_config(self):
        tl.save("v", dict(tl.load("v"), base="src"))  # a clone: reset gives it a blank disk of its own
        self.quiet(tl.reset, "v")
        self.assertNotIn("base", tl.load("v"))
        create = next(c for c in self.calls if c[1] == "create")
        self.assertEqual(create[1:4] + create[5:], ["create", "-f", "qcow2", "68719476736"])
        with open(os.path.join(self.d, "disk.qcow2")) as f:
            self.assertEqual(f.read(), "")  # replaced by the blank disk
        self.assertTrue(os.path.isfile(os.path.join(self.d, "machine.json")))

    def test_offline_snapshots_and_display(self):
        self.quiet(tl.snapshot, "v", "save", "clean")
        self.assertEqual(self.calls[-1][1:4], ["snapshot", "-c", "clean"])
        self.assertEqual(self.quiet(tl.snapshot, "v", "ls")["snapshots"], ["clean"])
        self.quiet(tl.display, "v", 1920, 1080)
        self.assertEqual((tl.load("v")["width"], tl.load("v")["height"]), (1920, 1080))
        with self.assertRaises(SystemExit):
            tl.display("v", 100, 100)

    def test_rm(self):
        self.quiet(tl.rm, "v")
        self.assertFalse(os.path.exists(self.d))

    def test_clone_shares_a_base(self):
        with self.assertRaises(SystemExit):  # snapshots would be stranded in the base
            tl.clone("v", "c")
        self.snaps = []
        os.makedirs(os.path.join(self.d, "answers"))
        self.quiet(tl.clone, "v", "c")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "c", "answers")))  # so reset reinstalls unattended
        bases = os.path.join(self.tmp, ".bases")
        base = os.path.join(bases, os.listdir(bases)[0])
        for vm in ("v", "c"):  # both are overlays on the read-only base, so both stay usable
            with open(os.path.join(self.tmp, vm, "disk.qcow2")) as f:
                self.assertEqual(f.read(), base)
        self.assertFalse(os.access(base, os.W_OK))
        self.assertEqual(tl.load("c")["base"], "v")
        self.assertTrue(tl.load("c")["installed"])
        self.assertNotIn("rename", tl.load("c"))  # not a Windows VM with the guest agent
        self.quiet(tl.clone, "v", "c2")  # v wrote nothing since: same base, no deeper chain
        self.assertEqual(os.listdir(bases), [os.path.basename(base)])
        with open(os.path.join(self.tmp, "c2", "disk.qcow2")) as f:
            self.assertEqual(f.read(), base)
        self.quiet(tl.rm, "c2")
        self.quiet(tl.snapshot, "v", "save", "x")  # in v's own overlay
        self.quiet(tl.rm, "c")
        self.assertTrue(os.path.isfile(base))  # v still needs it
        self.quiet(tl.reset, "v")
        self.assertEqual(os.listdir(bases), [])  # last user gone

    def test_sandbox(self):
        m = {"os": "win2025", "iso": "/x.iso", "memory_mb": 2048, "cpus": 2, "sandbox": True}
        os.makedirs(os.path.join(self.d, "answers"))
        setup = " ".join(tl.qemu_args("v", m, False))   # still installing: answers drive, real disk writes
        self.assertIn("vvfat", setup)
        self.assertNotIn("snapshot=on", setup)
        m["installed"] = True
        run = " ".join(tl.qemu_args("v", m, False))
        for needle in ("-nic none", "snapshot=on"):
            self.assertIn(needle, run)
        for needle in ("guest_agent", "vvfat", "user,model"):
            self.assertNotIn(needle, run)
        self.assertEqual("-sandbox on" in run, os.name != "nt")   # seccomp: QEMU on Linux hosts only
        plain = " ".join(tl.qemu_args("v", dict(m, sandbox=False), False))
        self.assertIn("guest_agent", plain)
        self.assertNotIn("snapshot=on", plain)
        self.assertTrue(self.quiet(tl.set_sandbox, "v", "on")["sandbox"])
        with self.assertRaises(SystemExit):
            tl.transfer("v", "push", "a", "b")
        old = tl.state
        tl.state = lambda n: "running"
        try:
            with self.assertRaises(SystemExit):   # changes are thrown away at stop: a snapshot now would be lost
                tl.snapshot("v", "save", "x")
            with self.assertRaises(SystemExit):
                tl.set_sandbox("v", "off")
        finally:
            tl.state = old
        self.assertFalse(self.quiet(tl.set_sandbox, "v", "off")["sandbox"])
        self.assertNotIn("sandbox", tl.load("v"))

    def test_resources(self):
        """Ceilings from the template (host-capped), live memory through the balloon, live CPUs on Linux only."""
        m = {"os": "linux", "iso": "/x.iso", "memory_mb": 2048, "cpus": 2, "balloon": True,
             "max_cpus": 4, "max_memory_mb": 6 * 1024}
        tl.save("v", m)
        args = " ".join(tl.qemu_args("v", m, False))
        self.assertIn(f"-m {min(6, tl.host_memory_gb()) * 1024}", args)          # RAM up to the ceiling...
        self.assertIn("virtio-balloon-pci", args)                               # ...given back by the balloon
        self.assertIn(f"-smp 2,maxcpus={min(4, os.cpu_count())}", args)
        win = " ".join(tl.qemu_args("v", dict(m, os="win2025", balloon=False), False))
        self.assertIn("-m 2048 ", win)                                          # no driver: exactly memory_mb
        self.assertIn("-smp 2 ", win)                                           # no live CPUs on Windows
        self.assertEqual(tl.limits({"os": "winxp"})[0], min(2, os.cpu_count()))  # template ceiling
        with self.assertRaises(SystemExit):
            tl.resources("v", cpus=99)
        out = tl.resources("v", cpus=1, memory=1)                               # stopped: saved for the next start
        self.assertEqual(out["applied"], {"memory": "saved", "cpus": "saved"})
        self.assertEqual((tl.load("v")["cpus"], tl.load("v")["memory_mb"]), (1, 1024))

        calls = []
        class FakeQMP:
            def cmd(self, c, **a):
                calls.append((c, a))
                if c == "query-hotpluggable-cpus":
                    return [{"type": "max-x86_64-cpu", "qom-path": "/machine/cpu0", "props": {"socket-id": 0}},
                            {"type": "max-x86_64-cpu", "props": {"socket-id": 1, "node-id": 0}},
                            {"type": "max-x86_64-cpu", "props": {"socket-id": 2}}]
                return {"actual": 3 * 2 ** 30} if c == "query-balloon" else {}
            def close(self): pass
        old = (tl.state, tl.qmp, tl._online_vcpus)
        tl.state, tl.qmp, tl._online_vcpus = (lambda n: "running"), (lambda n: FakeQMP()), (lambda n: True)
        try:
            out = tl.resources("v", cpus=2, memory=3)
            first = list(calls)
            tl._online_vcpus = lambda n: False   # no guest agent: added but offline, and the reply says so
            self.assertIn("offline", tl.resources("v", cpus=2)["applied"]["cpus"])
        finally:
            tl.state, tl.qmp, tl._online_vcpus = old
        self.assertEqual(out["applied"], {"memory": "now", "cpus": "now"})
        self.assertIn(("balloon", {"value": 3 * 2 ** 30}), first)
        self.assertIn(("device_add", {"driver": "max-x86_64-cpu", "id": "cpu1-0", "socket-id": 1, "node-id": 0}),
                      first)   # one more, with every property QEMU listed for that slot
        self.assertEqual(out["guest_memory_gb"], 3.0)

    def test_net_switch(self):
        tl.net_create("lab")
        sw, v, w, x = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4))
        for s in (sw, v, w, x):
            s.bind(("127.0.0.1", 0)); s.settimeout(2)
        sw.settimeout(0.2)
        t = threading.Thread(target=tl.net_switch, args=("lab", sw, lambda: False)); t.start()
        to, mv, mw, mx = sw.getsockname(), b"\x02" + b"v" * 5, b"\x02" + b"w" * 5, b"\x02" + b"x" * 5
        x.sendto(b"\xff" * 6 + mx + b"\x08\x06hello", to)     # x joins
        w.sendto(b"\xff" * 6 + mw + b"\x08\x06arp", to)       # broadcast from w: flooded to x
        self.assertEqual(x.recv(100)[6:12], mw)
        v.sendto(mw + mv + b"\x08\x00ping", to)                # unicast to w's MAC: only w gets it
        self.assertEqual(w.recv(100), mw + mv + b"\x08\x00ping")
        x.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            x.recv(100)
        t.join(5)
        self.assertFalse(t.is_alive())                             # no VM running: the switch exits
        for s in (sw, v, w, x):
            s.close()

    def test_networks(self):
        m = tl.load("v"); m.update(os="linux", iso="/x.iso"); tl.save("v", m)
        os.makedirs(os.path.join(self.tmp, "w")); tl.save("w", dict(m))
        lab = tl.net_create("lab")
        self.assertEqual((lab["index"], tl.net_create("dmz")["index"]), (1, 2))
        with self.assertRaises(SystemExit):
            tl.net_create("lab")
        with self.assertRaises(SystemExit):
            tl.net_create("Bad Name")
        a, b = tl.net_attach("v", "lab"), tl.net_attach("w", "lab")
        self.assertNotEqual((a["mac"], a["ip"]), (b["mac"], b["ip"]))
        self.assertEqual(tl.net_attach("v", "lab")["mac"], a["mac"])            # stable, idempotent
        args = " ".join(tl.qemu_args("v", tl.load("v"), False))
        self.assertIn(f"-netdev dgram,id=n-lab,local.type=inet,local.host=127.0.0.1,local.port={a['port']},"
                      "remote.type=inet,remote.host=127.0.0.1,remote.port=40001", args)  # loopback only
        self.assertNotEqual(a["port"], b["port"])
        self.assertIn(f"virtio-net-pci,netdev=n-lab,mac={a['mac']}", args)
        self.assertEqual([x["vm"] for x in tl.net_ls()[1]["vms"]], ["v", "w"])  # ls is sorted: dmz, lab
        with self.assertRaises(SystemExit):
            tl.net_rm("lab")                                                    # still has VMs
        old = tl.state
        tl.state = lambda n: "running"
        try:
            with self.assertRaises(SystemExit):
                tl.net_attach("v", "dmz")                                       # NICs change on a stopped VM
        finally:
            tl.state = old
        tl.net_attach("v", "lab", attach=False); tl.net_attach("w", "lab", attach=False)
        self.assertEqual(tl.net_rm("lab"), {"removed": "lab"})
        # internet: off for a sandbox unless the user turned it on for it
        self.quiet(tl.set_sandbox, "v", "on")
        self.assertFalse(tl.set_internet("v")["internet"])
        self.assertIn("-nic none", " ".join(tl.qemu_args("v", tl.load("v"), False)))
        self.assertTrue(tl.set_internet("v", "on")["internet"])
        self.assertIn("-nic user", " ".join(tl.qemu_args("v", tl.load("v"), False)))
        self.quiet(tl.set_sandbox, "v", "off"); self.quiet(tl.set_sandbox, "v", "on")
        self.assertFalse(tl.set_internet("v")["internet"])                     # approval does not carry over

    def test_top(self):
        if sys.platform != "darwin":                     # no /proc on macOS: top shows no figures there
            cpu, rss = tl._proc_times(os.getpid())      # this test process, on Linux and Windows alike
            self.assertTrue(cpu >= 0 and rss > 1 << 20)
        self.assertIsNone(tl._proc_times(2 ** 22 + 7))  # no such process
        out = tl.top()
        self.assertEqual(out["vms"], [])                 # nothing running here
        self.assertIn("cpus", out["quota"])

    def test_quota(self):
        """No command gets past the quota: start, more CPUs, clone and create are refused beyond it."""
        q = tl.quota()
        self.assertEqual((q["cpus"]["limit"], q["cpus"]["source"]), (64, "user"))
        self.assertEqual(q["vms"]["source"], "host default")
        self.assertEqual(q["disk_gb"]["used"], 64)                      # v's virtual size (qemu-img info, faked)
        tl.quota({"cpus": 3, "memory_gb": 4, "disk_gb": 100, "vms": 1})
        m = tl.load("v"); m.update(cpus=2, memory_mb=2048, os="linux", iso="/x.iso"); tl.save("v", m)
        os.makedirs(os.path.join(self.tmp, "w"))
        tl.save("w", dict(m, cpus=2))
        old = tl.state
        tl.state = lambda n: "running" if n == "w" else "stopped"      # w runs: 2 CPUs, 2 GiB, 1 VM
        try:
            with self.assertRaises(SystemExit) as e:
                tl.start("v")                                           # a second VM: vms 1 > 1, cpus 4 > 3
            self.assertIn("quota exceeded", str(e.exception))
            with self.assertRaises(SystemExit):
                tl.resources("w", cpus=4)                               # live CPUs: 2 + 2 > 3
        finally:
            tl.state = old
        with self.assertRaises(SystemExit):
            tl.clone("v", "c")                                          # 64 + 64 virtual GiB > 100
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "c")))
        with self.assertRaises(SystemExit):
            tl.quota({"cpus": 0})
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".quota.lock")))   # lock always released

    def test_sandbox_journal(self):
        """Every MCP action on a sandboxed VM lands in actions.log, failures included."""
        m = tl.load("v"); m["sandbox"] = True; tl.save("v", m)
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "click", "arguments": {"x": 5, "y": 6}}}
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(req) + "\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tl.mcp("v")   # the VM is not running: the click fails, and is still recorded
        finally:
            sys.stdin = old
        self.assertTrue(json.loads(out.getvalue())["result"]["isError"])
        with open(os.path.join(self.d, "actions.log"), encoding="utf-8") as f:
            entry = json.loads(f.read().splitlines()[0])
        self.assertEqual((entry["tool"], entry["arguments"]), ("click", {"x": 5, "y": 6}))
        self.assertIn("error", entry)

    def test_descriptions(self):
        """Phase 12: one line on each VM, network and project; the agent's own free, the user's with approval;
        never inherited by a clone, forgotten with the object."""
        asked = []
        def yes(msg, cli): asked.append(cli)
        def no(msg, cli): raise RuntimeError("declined: " + cli)
        self.snaps = []
        self.quiet(tl.main, ["describe", "vm", "v", "the docs build box"])
        self.assertEqual({r["name"]: r.get("description") for r in tl.vm_list()}, {"v": "the docs build box"})
        tl.agent_call("vm_clone", {"vm": "v", "new": "x", "description": "a throwaway copy"}, yes)
        self.assertEqual(tl.description("vm", "x"), "a throwaway copy")    # its own, not v's
        tl.agent_call("describe", {"kind": "vm", "name": "x", "description": "a test copy"}, no)
        tl.agent_call("net_create", {"net": "lab", "description": "x and its peers"}, no)
        self.assertEqual(tl.net_ls()[0]["description"], "x and its peers")
        with self.assertRaisesRegex(RuntimeError, "terminal-vm describe vm v 'my box'"):
            tl.agent_call("describe", {"kind": "vm", "name": "v", "description": "my box"}, no)
        tl.agent_call("describe", {"kind": "project", "name": "default", "description": "docs"}, yes)
        self.assertEqual(tl.project_status()["description"], "docs")
        self.assertEqual(asked, ["terminal-vm clone v x", "terminal-vm describe project default docs"])
        for bad in ("a\tb", "y" * 201):
            with self.assertRaisesRegex(SystemExit, "one line of at most 200"):
                tl.main(["describe", "vm", "v", bad])
        with self.assertRaisesRegex(SystemExit, "no VM named 'nope'"):
            tl.describe("vm", "nope", "z")
        self.quiet(tl.rm, "x"); self.quiet(tl.main, ["net", "rm", "lab"])
        self.assertEqual(tl.descriptions(), {"vm:v": "the docs build box", "project:default": "docs"})

    def test_infra(self):
        """The map: quota, each VM with its creator, managed, networks and addresses; networks with members;
        terminal-ct's map when it answers, a note when it does not; a project sees only its own."""
        yes = lambda m, c: None
        self.vm("job", project="alpha", created_by="agent", sandbox=True)
        tl.net_create("lab", "alpha"); tl.describe("net", "lab", "alpha's bench")
        tl.net_attach("job", "lab")
        tl.describe("vm", "v", "the user's desktop")
        ct = {"containers": [{"name": "web", "on": "this PC", "status": "running", "project": "alpha"}]}
        with mock.patch.object(tl, "ct_call", lambda args, timeout=60, **k: ct):
            m = tl.infra()
            text = tl.infra_text(m)
        vms = {v["name"]: v for v in m["vms"]}
        self.assertEqual((vms["job"]["managed"], vms["v"]["managed"], vms["v"]["created_by"]), (True, False, "user"))
        ip = vms["job"]["networks"]["lab"]
        self.assertTrue(ip.startswith("10.77."))
        self.assertEqual(m["networks"]["lab"]["members"], {"job": ip})
        self.assertEqual(m["networks"]["lab"]["description"], "alpha's bench")
        self.assertIn("free", m["this_pc"]["quota"]["cpus"])
        self.assertEqual(m["containers"], ct)
        for s in ("the user's desktop", f"job={ip}", "terminal-ct:", "web"):
            self.assertIn(s, text)
        def gone(args, timeout=60, **k):
            raise RuntimeError("terminal-ct not found: put it on the PATH, or set TERMINAL_CT")
        with mock.patch.object(tl, "ct_call", gone):
            scoped = tl.agent_call("infra", {}, yes, project="alpha")
        self.assertEqual([v["name"] for v in scoped["vms"]], ["job"])          # not the user's v (default)
        self.assertIn("terminal-ct not found", scoped["containers_note"])
        self.assertIn("used_by_project", scoped["this_pc"]["quota"]["cpus"])    # its own use, not the others'
        tl.infra_text(scoped)
        for reserved in ("networks", "images"):                                # STORE/NAME is a VM's folder
            with self.assertRaisesRegex(SystemExit, "invalid VM name"):
                tl.vm_dir(reserved)
        self.assertEqual([p["project"] for p in scoped["projects"]], ["alpha"])

    def test_agent_authority(self):
        """Agents run sandboxed VMs and their own; the user's VMs are off-limits; risky steps ask the user."""
        asked = []
        def yes(msg, cli): asked.append(cli)
        def no(msg, cli): raise RuntimeError("declined: " + cli)
        with self.assertRaises(SystemExit):
            tl.agent_call("vm_start", {"vm": "v"}, yes)                       # the user's VM
        with self.assertRaises(SystemExit):
            tl.agent_call("vm_create", {"vm": "x", "iso": "../../etc/passwd"}, yes)   # store images only
        self.snaps = []
        with self.assertRaises(RuntimeError):
            tl.agent_call("vm_clone", {"vm": "v", "new": "x"}, no)            # a copy of the user's VM: ask
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))
        tl.agent_call("vm_clone", {"vm": "v", "new": "x"}, yes)
        self.assertEqual((tl.load("x")["created_by"], tl.load("x")["offline"]), ("agent", True))
        tl.agent_call("vm_remove", {"vm": "x"}, no)
        asked.clear()
        m = tl.load("v"); m["sandbox"] = True; tl.save("v", m)               # a sandbox the user made
        self.assertTrue(tl.agent_call("vm_list", {}, yes)[0]["managed"])
        with self.assertRaises(RuntimeError):
            tl.agent_call("vm_internet", {"vm": "v", "on": True}, no)
        self.assertFalse(tl.load("v").get("internet"))
        self.assertTrue(tl.agent_call("vm_internet", {"vm": "v", "on": True}, yes)["internet"])
        self.assertEqual(asked, ["terminal-vm internet v on"])
        tl.net_create("mine")
        tl.agent_call("net_create", {"net": "lab"}, yes)
        tl.agent_call("net_attach", {"vm": "v", "net": "lab"}, no)            # the agent's network: no question
        with self.assertRaises(RuntimeError):
            tl.agent_call("net_attach", {"vm": "v", "net": "mine"}, no)       # the user's network
        with self.assertRaises(SystemExit):
            tl.agent_call("net_remove", {"net": "mine"}, yes)
        tl.agent_call("net_detach", {"vm": "v", "net": "lab"}, no)
        tl.agent_call("net_remove", {"net": "lab"}, no)
        with self.assertRaises(RuntimeError):
            tl.agent_call("vm_remove", {"vm": "v"}, no)                       # the user's sandbox: ask first
        self.assertTrue(os.path.exists(self.d))
        m = tl.load("v"); m.pop("sandbox"); m["created_by"] = "agent"; tl.save("v", m)
        tl.agent_call("vm_remove", {"vm": "v"}, no)                           # its own VM: no question
        self.assertFalse(os.path.exists(self.d))

    def test_mcp_elicitation(self):
        """A risky tool asks the user through the client and runs only once they accept."""
        m = tl.load("v"); m["created_by"] = "agent"; tl.save("v", m)
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {"elicitation": {}}}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "vm_internet", "arguments": {"vm": "v", "on": True}}},
                 {"jsonrpc": "2.0", "id": 3, "method": "ping"},                # arrives while the user decides
                 {"jsonrpc": "2.0", "id": "ask-1", "result": {"action": "accept", "content": {"approve": True}}}]
        old = sys.stdin
        sys.stdin = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tl.mcp("v")
        finally:
            sys.stdin = old
        msgs = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(msgs[1]["method"], "elicitation/create")
        self.assertEqual(json.loads(msgs[2]["result"]["content"][0]["text"]), {"vm": "v", "internet": True})
        self.assertEqual(msgs[3]["id"], 3)                                   # the queued ping is answered after
        with open(os.path.join(self.tmp, ".projects", "default", "agent.log"), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["tool"], "vm_internet")
        os.makedirs(os.path.join(self.tmp, "u")); tl.save("u", {"cpus": 1})  # the user's VM
        sys.stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {
            "name": "screenshot", "arguments": {"vm": "u"}}}) + "\n")
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tl.mcp("v")
        finally:
            sys.stdin = old
        self.assertIn("belongs to the user", out.getvalue())                 # screen tools too
        sys.stdin = io.StringIO(json.dumps(lines[1]) + "\n")                # a client that cannot ask: refused
        m = tl.load("v"); m["offline"] = True; tl.save("v", m)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                tl.mcp("v")
        finally:
            sys.stdin = old
        self.assertIn("terminal-vm internet v on", out.getvalue())
        self.assertTrue(tl.load("v")["offline"])

    def test_install_cache_until_installed(self):
        m = {"os": "win2025", "iso": "/x.iso", "memory_mb": 2048, "cpus": 2}
        os.makedirs(os.path.join(self.d, "answers"))
        self.assertIn("cache=unsafe", " ".join(tl.qemu_args("v", m, False)))
        m["installed"] = True
        self.assertNotIn("cache=unsafe", " ".join(tl.qemu_args("v", m, False)))


class GuestAgent(unittest.TestCase):
    """The guest agent client."""
    def test_agent_resync_after_stale_reply(self):
        class Sock:  # the guest agent answers each request in order; an earlier client left one reply unread
            def __init__(self): self.out = [b'{"return": 7}\n', b'{"error": {"desc": "late set-time"}}\n']
            def sendall(self, data):
                req = json.loads(data)
                self.out.append(json.dumps({"return": req["arguments"]["id"] if req["execute"] == "guest-sync"
                                            else {"pid": 42}}).encode() + b"\n")
            def recv(self, n): return self.out.pop(0) if self.out else b""
            def close(self): pass
        with mock.patch.object(tl, "connect", lambda *a: Sock()), mock.patch.object(tl, "endpoint", lambda *a: "x"):
            self.assertEqual(tl.QGA("a").call("guest-exec", path="/bin/true"), {"pid": 42})  # not a sync's answer

    def test_exec_waits_for_a_busy_agent(self):
        """One client at a time: a command right after a node's ready file waits for the identity step's
        connection instead of failing; a connection whose sync went unanswered is closed (QEMU's one queued slot)."""
        closed, tries = [], []
        class Sock:
            def __init__(self, busy): self.busy, self.out = busy, []
            def settimeout(self, t): pass
            def sendall(self, data):
                req = json.loads(data)
                if self.busy:
                    return
                r = {"guest-sync": req.get("arguments", {}).get("id"), "guest-exec": {"pid": 7},
                     "guest-exec-status": {"exited": True, "exitcode": 0, "out-data": "bjEK"}}[req["execute"]]
                self.out.append(json.dumps({"return": r}).encode() + b"\n")
            def recv(self, n):
                if self.busy:
                    raise socket.timeout("timed out")
                return self.out.pop(0)
            def close(self): closed.append(self.busy)
        def conn(*a):
            tries.append(1)
            if len(tries) == 1:
                raise BlockingIOError(11, "Resource temporarily unavailable")  # the queue is full
            return Sock(busy=len(tries) == 2)                                  # queued, never served in time
        with mock.patch.object(tl, "connect", conn), mock.patch.object(tl, "endpoint", lambda *a: "x"), \
                mock.patch.object(tl.time, "sleep", lambda s: None):
            self.assertEqual(tl.guest_exec("a", "hostname", []), {"exit": 0, "stdout": "n1\n", "stderr": ""})
        self.assertEqual((len(tries), closed), (3, [True, False]))
        with mock.patch.object(tl, "connect", lambda *a: (_ for _ in ()).throw(ConnectionRefusedError())), \
                mock.patch.object(tl, "endpoint", lambda *a: "x"), mock.patch.object(tl.time, "sleep", lambda s: None):
            with self.assertRaises(tl.AgentBusy):
                tl.guest_exec("a", "hostname", [], timeout=0)


class Doctor(unittest.TestCase):
    """Phase 13.1: known problems with their fix, a masked report (VM passwords too)."""
    def test_report_masks_and_checks_have_fixes(self):
        tl.STORE = tempfile.mkdtemp()
        os.makedirs(os.path.join(tl.STORE, "v"))
        tl.save("v", {"cpus": 1, "memory_mb": 1024, "password": "Pw-secret-42"})
        with open(os.path.join(tl.STORE, "events.log"), "w") as f:
            f.write(json.dumps({"event": "created", "detail": "password Pw-secret-42 set"}) + "\n")
        checks = tl.doctor_checks()
        self.assertTrue(all({"check", "ok", "detail", "required"} <= set(c) for c in checks))
        self.assertFalse([c for c in checks if c["ok"] and "fix" in c])
        with open(tl.doctor_report(), encoding="utf-8") as f:
            report = f.read()
        self.assertNotIn("Pw-secret-42", report)
        self.assertIn("== events.log", report)
        self.assertNotIn(os.path.expanduser("~"), tl.mask(os.path.expanduser("~") + "/x token=abc"))


class Installer(unittest.TestCase):
    """Phase 13.2: install.sh into a throwaway HOME (no packages, no agent), update, uninstall."""
    SH = os.path.join(os.path.dirname(os.path.abspath(tl.__file__)), "install.sh")

    @unittest.skipIf(tl.WINDOWS or not shutil.which("sh"), "a POSIX shell")
    def test_install_update_uninstall(self):
        home = tempfile.mkdtemp()
        with open(os.path.join(home, ".profile"), "w") as f:
            f.write("echo mine\n")
        env = dict(os.environ, HOME=home, PATH="/usr/bin:/bin")
        run = lambda *a: subprocess.run(["sh", self.SH, *a, "--no-deps", "--no-mcp"], env=env, capture_output=True,
                                        text=True, stdin=subprocess.DEVNULL, timeout=120)
        r = run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tool = os.path.join(home, ".local", "bin", "terminal-vm")
        self.assertIn(tl.VERSION, subprocess.run([tool, "version"], capture_output=True, text=True).stdout)
        self.assertEqual(run("--update").returncode, 0)
        self.assertEqual(run("--uninstall").returncode, 0)
        self.assertFalse(os.path.exists(tool))
        with open(os.path.join(home, ".profile")) as f:
            self.assertNotIn("terminal-vm", f.read())


class Supervision(unittest.TestCase):
    """The daemon's passes: restart with growing delays, guest agent health, idle pause and wake, events."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); tl.STORE = self.tmp
        for n in ("a", "b"):
            os.makedirs(os.path.join(self.tmp, n))
            tl.save(n, {"cpus": 1, "memory_mb": 1024, "os": "linux"})
        self.states = {"a": "stopped", "b": "stopped"}
        self.started, self.qmp_cmds, self.agent = [], [], {"a": True, "b": True}
        self.cpu = {"a": 0.0, "b": 0.0}
        test = self

        class Q:
            def cmd(self, c, **k):
                test.qmp_cmds.append(c)
                if c == "stop": test.states["a"] = "paused"
                if c == "cont": test.states["a"] = "running"
                return {"status": test.states["a"]} if c == "query-status" else {}
            def close(self): pass

        class Agent:
            def __init__(self, n, timeout=0.5):
                if not test.agent[n]: raise OSError("no agent")
            def call(self, *a, **k): return {}

        def start(n, window=False, set_want=True):
            if n == "b": raise SystemExit("quota exceeded")
            self.started.append(n); self.states[n] = "running"; print(json.dumps({"started": n}))
        self.disk_held = set()
        self.real_disk_in_use = tl.disk_in_use
        self.patches = [mock.patch.object(tl, "state", lambda n: self.states[n]), mock.patch.object(tl, "start", start),
                        mock.patch.object(tl, "qmp", lambda n: Q()), mock.patch.object(tl, "QGA", Agent),
                        mock.patch.object(tl, "_pid", lambda n: 1),
                        mock.patch.object(tl, "disk_in_use", lambda n: n in self.disk_held),
                        mock.patch.object(tl, "_proc_times", lambda pid: (self.cpu["a"], 0))]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()

    def test_restart_with_growing_delays(self):
        st = {}
        tl.want("a", "running"); tl.want("b", "stopped")
        out = tl.supervise_pass(st, now=1000)
        self.assertEqual((self.started, [e["event"] for e in out]), (["a"], ["restarted"]))  # b: stopped on purpose
        self.states["a"] = "stopped"                                       # crashed again at once
        self.assertEqual(tl.supervise_pass(st, now=1001), [])               # waits: 5 s, then 10, 20...
        tl.supervise_pass(st, now=1006)
        self.assertEqual(self.started, ["a", "a"])
        self.assertEqual(st["a"]["next_try"], 1016)
        tl.supervise(  "a", restart="never"); self.states["a"] = "stopped"
        self.assertEqual(tl.supervise_pass(st, now=5000), [])                # never: left down
        tl.want("b", "running")
        self.assertEqual([e["event"] for e in tl.supervise_pass(st, now=5000)], ["restart_failed"])  # quota: later
        tl.want("b", "stopped")
        tl.supervise("a", restart="always"); tl.want("a", "running")
        self.disk_held.add("a")                                            # QMP busy, QEMU alive: not a crash
        self.assertEqual(tl.supervise_pass(st, now=6000), [])
        self.disk_held.clear()
        open(os.path.join(self.tmp, "a", "window"), "w").close()           # its window was closed
        self.assertEqual([e["event"] for e in tl.supervise_pass(st, now=6000)], ["window_closed"])
        self.assertEqual(tl.wanted("a"), "stopped")
        tl.want("a", "running")
        with tl.vm_op("a"):                                                # a user operation on it: next pass
            self.assertEqual(in_thread(lambda: tl.supervise_pass(st, now=6000)), [])  # another thread waits for it
        self.assertEqual([e["event"] for e in tl.events()][-2:], ["restart_failed", "window_closed"])
        self.assertEqual(tl.events(vm="a")[0]["event"], "restarted")

    def test_clock_set_after_pause_and_sleep(self):
        test, calls = self, []

        class Clock:  # a guest whose clock stopped for an hour (idle pause, host asleep)
            def __init__(self, n, timeout=0.5): pass
            def call(self, c, **k):
                calls.append((c, k))
                return test.guest_ns if c == "guest-get-time" else {}
        self.guest_ns = time.time_ns() - 3600 * 10 ** 9
        with mock.patch.object(tl, "QGA", Clock):
            self.assertAlmostEqual(tl.sync_clock("a"), 3600, delta=5)
            self.assertEqual(calls[-1][0], "guest-set-time")
            self.assertLess(abs(calls[-1][1]["time"] - time.time_ns()), 5 * 10 ** 9)
            self.guest_ns, n = time.time_ns() - 10 ** 8, len(calls)          # 0.1 s off: left alone
            self.assertEqual(tl.sync_clock("a"), 0)
            self.assertEqual([c for c, _ in calls[n:]], ["guest-get-time"])
            self.guest_ns = time.time_ns() - 3600 * 10 ** 9                  # woken from an idle pause
            self.states["a"] = "paused"
            open(tl._idle_marker("a"), "w").close()
            tl.wake("a")
            self.assertEqual([e["event"] for e in tl.events(vm="a")][-2:], ["woken", "clock_set"])
            self.guest_ns, st = time.time_ns() - 7200 * 10 ** 9, {}           # the host slept: the daemon's check
            used = os.path.join(self.tmp, "a", "last_use")                   # (not right after a use: the agent's
            os.utime(used, (time.time() - 120, time.time() - 120))           # one connection may be taken)
            out = tl.supervise_pass(st, now=time.time())
            self.assertEqual([e["event"] for e in out], ["clock_set"])
            self.assertEqual(tl.supervise_pass(st, now=time.time() + 10), [])  # once a minute, not every pass

    def test_health_and_idle_pause(self):
        st, cpu, T = {}, {}, 100000
        self.states["a"] = "running"
        tl.supervise_pass(st, now=T + 0, cpu=cpu)                               # agent answers: known to have one
        self.agent["a"] = False
        outs = [tl.supervise_pass(st, now=t, cpu=cpu) for t in (T + 10, T + 400, T + 410, T + 420)]
        self.assertEqual([[e["event"] for e in o] for o in outs], [[], [], [], ["unhealthy"]])  # after the grace, 3 misses
        self.agent["a"] = True
        self.assertEqual([e["event"] for e in tl.supervise_pass(st, now=T + 430, cpu=cpu)], ["healthy"])
        # idle: QEMU's CPU time unchanged between passes (0 %) for idle_pause minutes -> paused, marker for wake
        tl.supervise("a", idle_pause=1)
        self.assertEqual([tl.supervise_pass(st, now=t, cpu=cpu) for t in (T + 500, T + 510, T + 560)], [[], [], []])
        out = tl.supervise_pass(st, now=T + 570, cpu=cpu)
        self.assertEqual([e["event"] for e in out], ["idle_paused"])
        self.assertEqual(self.states["a"], "paused")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a", "idle_paused")))
        tl.wake("a")                                                       # the next action
        self.assertEqual((self.states["a"], self.qmp_cmds[-1]), ("running", "cont"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "idle_paused")))
        for t in (T + 600, T + 700, T + 800):                                          # computing: 30 CPU s per 100 s = 30 %
            self.cpu["a"] += 30.0
            self.assertEqual(tl.supervise_pass(st, now=t, cpu=cpu), [])
        used = os.path.join(self.tmp, "a", "last_use")                     # touched by wake: a use
        self.assertTrue(os.path.exists(used))
        self.assertEqual(tl.supervise_pass(st, now=T + 910, cpu=cpu), [])       # quiet from 910...
        os.utime(used, (T + 960, T + 960))                                         # ...but used at 960
        self.assertEqual(tl.supervise_pass(st, now=T + 975, cpu=cpu), [])       # 65 s after 910, 15 after the use
        self.assertEqual([e["event"] for e in tl.supervise_pass(st, now=T + 1025, cpu=cpu)], ["idle_paused"])
        tl.wake("a")
        with contextlib.redirect_stdout(io.StringIO()):
            tl.pause("a")                                                  # the user's pause: not woken
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", "idle_paused")))
        tl.wake("a")
        self.assertEqual(self.states["a"], "paused")

    def test_one_vm_failing_does_not_stop_the_pass(self):
        tl.want("a", "running"); tl.want("b", "running")
        old = tl.load
        with mock.patch.object(tl, "load", lambda n: (_ for _ in ()).throw(ValueError("broken")) if n == "a" else old(n)):
            out = tl.supervise_pass({}, now=1000)
        self.assertEqual([(e["event"], e["vm"]) for e in out], [("supervise_failed", "a"), ("restart_failed", "b")])

    def test_vm_op_lock(self):
        with tl.vm_op("a"):
            with tl.vm_op("a"):                                            # re-entrant in one process
                pass
            self.assertTrue(os.path.isdir(os.path.join(self.tmp, "a", ".op.lock")))
            with self.assertRaisesRegex(SystemExit, "busy"):               # another thread (a web click) waits
                in_thread(lambda: tl.vm_op("a", wait=0.3).__enter__())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "a", ".op.lock")))

    def test_long_use_and_daemon_lock(self):
        used = os.path.join(self.tmp, "a", "last_use")
        with tl.in_use("a"):                                               # an exec or a file copy
            os.utime(used, (1, 1))
        self.assertGreater(os.path.getmtime(used), 1000)                   # fresh again at its end
        lock = tl._claim_daemon_lock()
        self.assertTrue(lock and os.path.isdir(lock))
        with mock.patch.object(tl.time, "sleep", lambda s: None):
            self.assertIsNone(tl._claim_daemon_lock())                     # young lock, no beat: being started
            os.utime(lock, (1, 1))                                         # old and no beat: left by a dead daemon
            self.assertEqual(tl._claim_daemon_lock(), lock)                # taken over (a reused pid is no proof)

    @unittest.skipIf(tl.WINDOWS, "the image lock probe is for Linux hosts")
    def test_disk_in_use_needs_a_lock_message(self):
        open(os.path.join(self.tmp, "a", "disk.qcow2"), "w").close()
        for err, held in (("Failed to get shared \"write\" lock", True), ("Could not open: corrupt image", False)):
            with mock.patch.object(tl, "qemu_bin", lambda n: n), \
                    mock.patch.object(tl.subprocess, "run", lambda *a, **k: tl.subprocess.CompletedProcess(a, 1, "", err)):
                self.assertEqual(self.real_disk_in_use("a"), held)          # only a lock error means "in use"

    def test_settings_and_status(self):
        self.assertEqual(tl.supervise("a"), {"name": "a", "restart": "always", "idle_pause": 0})
        with self.assertRaises(SystemExit):
            tl.supervise("a", restart="sometimes")
        s = tl.daemon("status")
        self.assertEqual((s["daemon"], [v["name"] for v in s["supervised"]]), ("stopped", ["a", "b"]))


class SharedDashboard(unittest.TestCase):
    """The web dashboard's code is one block, identical in terminal-ct and terminal-vm: change it in both, then set
    SHARED_SHA in both tests to the new value."""
    SHARED_SHA = "1438c234262934d5b591839c36d218bb09d2204f8bc14b6e2a6d0666630ab50f"

    @staticmethod
    def block(path):
        text = open(path, encoding="utf-8").read().replace("\r\n", "\n")
        return text[text.index("# ===== shared web dashboard"):text.index("# ===== end of shared web dashboard =====")]

    def test_block_is_the_shared_one(self):
        import hashlib
        mine = self.block(tl.__file__)
        self.assertEqual(hashlib.sha256(mine.encode()).hexdigest(), self.SHARED_SHA,
                         "the shared dashboard block changed: copy it to the other tool and update SHARED_SHA in both")
        other = os.environ.get("TERMINAL_CT_SRC") or os.path.join(os.path.dirname(os.path.abspath(tl.__file__)), "..", "..", "terminal-conteneur", "terminal_ct.py")
        if os.path.isfile(other):  # both checked out side by side: compare them directly
            self.assertEqual(mine, self.block(other))


class WebDashboard(unittest.TestCase):
    """terminal-vm's side of the shared dashboard: VMs here, containers through terminal-ct's command line."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); tl.STORE = self.tmp
        for n, p in (("a", "default"), ("b", "lab")):
            os.makedirs(os.path.join(self.tmp, n))
            tl.save(n, {"cpus": 1, "memory_mb": 1024, "os": "linux", "project": p})
        self.ct = os.path.join(self.tmp, "fake_ct.py")
        with open(self.ct, "w") as f:
            f.write("import json, sys\na = sys.argv[1:]\n"
                    "open(sys.argv[0] + '.log', 'a').write(json.dumps(a) + '\\n')\n"
                    "if a[:2] == ['dash', '--json']: print(json.dumps({'engine': 'podman', 'daemon': True, "
                    "'rows': [{'name': 'web1'}], 'stacks': [], 'events': [{'time': 't', 'event': 'started'}]}))\n"
                    "elif a[0] == 'stack': print(json.dumps({'stack': a[2], 'project': 'default', "
                    "'services': {'app': {'image': 'nginx', 'replicas': 2, 'running': 1}}}))\n"
                    "elif a[0] == 'logs': print('line 1')\n"
                    "elif a[0] == 'apply': print(json.dumps(json.load(open(a[1]))))\n"
                    "elif a[0] in ('start', 'stop', 'rm', 'scale'): print(json.dumps({a[0]: a[1:]}))\n"
                    "else: sys.stderr.write(json.dumps({'error': 'no such container'}) + '\\n'); sys.exit(1)\n")
        self.patches = [mock.patch.object(tl, "state", lambda n: "stopped"),
                        mock.patch.dict(os.environ, {"TERMINAL_CT": f'"{sys.executable}" "{self.ct}"'})]
        for p in self.patches: p.start()
        tl._CT_CACHE.clear()

    def tearDown(self):
        for p in self.patches: p.stop()

    def calls(self):
        with open(self.ct + ".log") as f:
            return [json.loads(l) for l in f]

    def test_api(self):
        api = lambda m, path, q=None, body=None: tl.web_api(m, path, q or {}, body)[1]
        d = api("GET", "/api/state")
        self.assertEqual((d["tool"], d["engine"], d["rows"]), ("terminal-vm", "podman", [{"name": "web1"}]))
        self.assertEqual(self.calls()[-1], ["dash", "--json"])
        api("GET", "/api/state", {"project": "lab"})
        self.assertEqual(self.calls()[-1], ["dash", "--json", "--project", "lab"])
        n = len(self.calls())
        api("GET", "/api/state", {"project": "lab"})                                   # another tab: cached
        self.assertEqual(len(self.calls()), n)
        self.assertEqual([v["name"] for v in api("GET", "/api/vms")["vms"]], ["a", "b"])
        self.assertEqual([v["name"] for v in api("GET", "/api/vms", {"project": "lab"})["vms"]], ["b"])
        with mock.patch.object(tl, "quota", lambda: {"cpus": {}}):  # the real one measures disks with qemu-img
            self.assertIn("cpus", api("GET", "/api/quota")["quota"])
        self.assertEqual(api("GET", "/api/logs", {"name": "web1"}), b"line 1\n")
        self.assertEqual(api("GET", "/api/stack", {"name": "lab"})["desired"]["services"],
                         {"app": {"image": "nginx", "replicas": 2}})                    # running is status, not desired
        self.assertEqual(api("POST", "/api/do", body={"op": "start", "name": "web1"}), {"start": ["web1"]})
        api("GET", "/api/state", {"project": "lab"})                                   # an action: fresh state
        self.assertEqual(self.calls()[-1], ["dash", "--json", "--project", "lab"])
        self.assertEqual(api("POST", "/api/do", body={"op": "scale", "stack": "lab", "service": "app", "replicas": 3}),
                         {"scale": ["lab", "app", "3"]})
        spec = {"stack": "lab", "services": {"app": {"image": "nginx"}}}
        self.assertEqual(api("POST", "/api/do", body={"op": "apply", "stack": spec}), spec)
        self.assertEqual(os.listdir(os.path.join(self.tmp, ".web")), [])               # its temporary file removed
        for bad in ({"op": "rm", "name": "-rf"}, {"op": "start", "name": "../x"}, {"op": "scale", "stack": "a b"},
                    {"kind": "vm", "op": "rm", "name": "a"}, {"op": "format"}):
            with self.assertRaises((ValueError, SystemExit)):
                api("POST", "/api/do", body=bad)
        with self.assertRaisesRegex(RuntimeError, "no such container"):
            api("GET", "/api/shot", {"name": "nope"})
        with mock.patch.object(tl, "start", lambda n, window=False, set_want=True: print(json.dumps({"started": n}))):
            self.assertEqual(api("POST", "/api/do", body={"kind": "vm", "op": "start", "name": "a"}), {"started": "a"})
        with self.assertRaises(tl.NoRoute):
            api("GET", "/api/nothing")

    def test_without_terminal_ct(self):
        with mock.patch.dict(os.environ, {"TERMINAL_CT": ""}), mock.patch.object(tl.shutil, "which", lambda e: None):
            d = tl.web_api("GET", "/api/state", {}, None)[1]
        self.assertEqual((d["rows"], d["tool"]), ([], "terminal-vm"))
        self.assertIn("terminal-ct not found", d["note"])


class Bench(unittest.TestCase):
    """bench: every step timed on a clone of SRC; the clone removed after, even when a step fails."""

    @staticmethod
    def write(path, text):
        with open(path, "w") as f:
            f.write(text)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.running, self.log, self.fail_start = set(), [], 0
        os.makedirs(os.path.join(self.tmp, "src"))
        write = self.write
        write(os.path.join(self.tmp, "src", "machine.json"), '{"os": "linux"}')
        self.fail_rm, self.fail_shot = False, False

        def clone(src, new):
            if os.path.exists(os.path.join(self.tmp, new)):
                raise SystemExit(f"VM {new!r} already exists")
            os.makedirs(os.path.join(self.tmp, new)); write(os.path.join(self.tmp, new, "machine.json"), "{}")
            self.log.append("clone")

        def screenshot(n, file=""):
            if self.fail_shot:
                raise RuntimeError("QMP gone")
            return b"png", (1, 1)

        def start(n, set_want=True):
            self.assertFalse(set_want)                                   # bench's clone is never supervised
            self.fail_start -= 1
            if self.fail_start == 0:
                raise SystemExit("quota")
            self.running.add(n); self.log.append("start")

        def rm(n):
            if n in self.running or self.fail_rm:
                raise SystemExit(f"{n} is running")
            import shutil; shutil.rmtree(os.path.join(self.tmp, n)); self.log.append("rm")

        def stop(n, force=False):
            self.assertTrue(force); self.running.discard(n); self.log.append("stop")
        self.patches = [mock.patch.object(tl, "STORE", self.tmp), mock.patch.object(tl, "clone", clone),
                        mock.patch.object(tl, "start", start), mock.patch.object(tl, "rm", rm),
                        mock.patch.object(tl, "stop", stop),
                        mock.patch.object(tl, "state", lambda n: "running" if n in self.running else "stopped"),
                        mock.patch.object(tl, "disk_in_use", lambda n: n in self.running),
                        mock.patch.object(tl, "screenshot", screenshot),
                        mock.patch.object(tl, "pause", lambda n, resume=False: None),
                        mock.patch.object(tl, "vm_list", lambda: []), mock.patch.object(tl, "_vm_rows", lambda: [])]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_timings(self):
        r = tl.bench(3, "src")
        self.assertEqual(set(r["results"]), {"clone", "start", "screenshot", "pause", "resume", "ls", "dashboard_data",
                                             "stop_force", "rm"})
        self.assertEqual(r["results"]["start"]["runs"], 3)
        self.assertEqual(r["from"], "src (linux)")
        self.assertEqual(sorted(os.listdir(self.tmp)), ["src"])

    def test_clone_removed_when_a_step_fails(self):
        self.fail_start = 2                                              # the second start fails
        with self.assertRaisesRegex(SystemExit, "quota"):
            tl.bench(3, "src")
        self.assertEqual(self.log[-2:], ["clone", "rm"])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["src"])

    def test_running_clone_stopped_then_removed_and_error_kept(self):
        self.fail_shot = True                                            # fails while the clone runs
        with self.assertRaisesRegex(RuntimeError, "QMP gone"):
            tl.bench(2, "src")
        self.assertEqual(self.log, ["clone", "start", "stop", "rm"])
        self.fail_shot, self.fail_rm, self.log = True, True, []          # cleanup failing too: the step's error shows
        with self.assertRaisesRegex(RuntimeError, "QMP gone"):
            tl.bench(2, "src")

    def test_never_touches_an_existing_vm(self):
        with mock.patch.object(tl.secrets, "token_hex", lambda n: "abcd"):
            mine = os.path.join(self.tmp, f"bench-{os.getpid()}-abcd")
            os.makedirs(mine); self.write(os.path.join(mine, "machine.json"), "{}")   # a VM with that very name
            with self.assertRaisesRegex(SystemExit, "already exists"):
                tl.bench(1, "src")
        self.assertTrue(os.path.exists(os.path.join(mine, "machine.json")))
        self.assertEqual(self.log, [])

    def test_linux_source_only(self):
        self.write(os.path.join(self.tmp, "src", "machine.json"), '{"os": "win11"}')
        with self.assertRaisesRegex(SystemExit, "Linux"):
            tl.bench(1, "src")

    def test_summary(self):
        self.assertEqual(tl._summary([3, 1, 2, 10]), {"median_ms": 2.5, "min_ms": 1, "max_ms": 10, "runs": 4})


class CloudImages(unittest.TestCase):
    """create --image: an official cloud image under an overlay, set up by cloud-init from a cidata FAT disk."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = []
        base = os.path.join(self.tmp, "images", "debian-13-genericcloud-amd64-0123456789ab.qcow2")
        self.patches = [mock.patch.object(tl, "STORE", self.tmp), mock.patch.object(tl, "qemu_bin", lambda t: t),
                        mock.patch.object(tl.subprocess, "run", lambda cmd, **k: self.calls.append(cmd)),
                        mock.patch.object(tl, "cloud_image", lambda kind: base)]
        for p in self.patches: p.start()
        with open(os.path.join(self.tmp, "quota.json"), "w") as f:
            json.dump({"disk_gb": 1000, "memory_gb": 64, "cpus": 64}, f)

    def tearDown(self):
        for p in self.patches: p.stop()

    def seed(self, name):
        d = os.path.join(self.tmp, name, "cidata")
        with open(os.path.join(d, "user-data")) as f:
            head, body = f.read().split("\n", 1)
        with open(os.path.join(d, "meta-data")) as f:
            return head, json.loads(body), f.read()

    def test_debian_node(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tl.create("n1", None, cpus=2, memory=2, disk=10, image="debian", node=True, template=False)  # its build
        reply = json.loads(out.getvalue())
        m = tl.load("n1")
        self.assertNotIn("iso", m)
        self.assertEqual((m["image"], m["cloud"], m["node"], m["user"]), ("debian", True, True, "user"))
        self.assertEqual(reply["password"], m["password"])
        self.assertIn("-b", self.calls[-1]); self.assertIn("10G", self.calls[-1])    # an overlay on the image
        head, cfg, meta = self.seed("n1")
        self.assertEqual(head, "#cloud-config")
        self.assertEqual(cfg["packages"], ["qemu-guest-agent", "docker.io", "python3"])
        self.assertEqual(reply["ready_file"], m["ready_file"])
        self.assertTrue(m["ready_file"].endswith(m["instance_id"]))                    # per instance: a clone's own
        self.assertTrue(cfg["runcmd"][-1].startswith("docker info >/dev/null 2>&1 && "))  # only once Docker answers
        self.assertTrue(cfg["runcmd"][-1].endswith("touch " + m["ready_file"]))
        self.assertEqual(cfg["chpasswd"]["users"][0]["password"], m["password"])
        self.assertIn('local-hostname: "n1"', meta)
        self.assertIn(f'instance-id: "{m["instance_id"]}"', meta)
        args = " ".join(tl.qemu_args("n1", m, False))
        self.assertNotIn("-cdrom", args)
        self.assertIn("driver=vvfat,dir=" + os.path.join(self.tmp, "n1", "cidata") + ",label=cidata,rw=off", args)

    def test_alpine_and_plain(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tl.create("a_1", None, cpus=1, memory=1, disk=4, image="alpine", node=True, template=False)
            tl.create("plain", None, cpus=1, memory=1, disk=4, image="debian")
        _, cfg, meta = self.seed("a_1")
        self.assertEqual(cfg["packages"], ["qemu-guest-agent", "docker", "python3"])
        rc = cfg["runcmd"]
        self.assertEqual([c.split()[0] for c in rc], ["rc-update", "rc-service", "rc-update", "for", "docker"])
        self.assertIn("cgroups", rc[1]); self.assertIn("docker", rc[2])                # cgroups up before Docker
        self.assertIn("docker info", rc[3])                                             # ready once Docker answers
        self.assertNotIn("sudo", cfg["users"][0])                                     # Alpine has doas
        self.assertIn('local-hostname: "a-1"', meta)                                   # no _ in host names
        plain = self.seed("plain")[1]
        self.assertEqual(plain["packages"], ["qemu-guest-agent"])                      # not a node: just the agent
        self.assertTrue(plain["runcmd"][-1].startswith("mkdir"))                       # no Docker to wait for

    def test_template_node(self):
        tpl = os.path.join(self.tmp, "images", "node-generic_alpine-3.22.2-x86_64-bios-cloudinit-r0-ab-t1.qcow2")
        os.makedirs(os.path.dirname(tpl)); open(tpl, "w").close()
        info = {"virtual-size": 4 * 2 ** 30}

        def run(cmd, **k):
            self.calls.append(cmd)
            if cmd[:2] == ["qemu-img", "create"]:
                open(cmd[-2], "w").close()
            return mock.Mock(stdout=json.dumps(info), returncode=0)
        with mock.patch.object(tl, "node_template", lambda kind: tpl), mock.patch.object(tl.subprocess, "run", run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            tl.create("n_2", None, cpus=1, memory=1, disk=12, image="alpine", node=True)
        reply, m = json.loads(out.getvalue()), tl.load("n_2")
        self.assertEqual((m["template"], m["identity"], m["node"]), (tpl, True, True))
        self.assertNotIn("cloud", m)
        self.assertIn(tpl, self.calls[-1]); self.assertIn("12G", self.calls[-1])          # an overlay, grown
        self.assertIn("about 15 s", reply["note"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "n_2", "cidata")))           # no cloud-init at all
        self.assertNotIn("vvfat", " ".join(tl.qemu_args("n_2", m, False)))
        script = tl.node_identity_script("n_2", m)
        for part in ("echo n-2 > /etc/hostname", "adduser -D -s /bin/ash user", "| chpasswd", "resize2fs", "docker info",
                     "touch " + m["ready_file"]):
            self.assertIn(part, script)
        deb = tl.node_identity_script("d1", dict(m, image="debian"))
        self.assertIn("useradd -m -s /bin/bash user", deb); self.assertIn("NOPASSWD", deb); self.assertIn("growpart", deb)
        first = m["ready_file"]
        m.pop("identity"); tl.save("n_2", m)                                                 # its first start done
        with mock.patch.object(tl.subprocess, "run", run), mock.patch.object(tl, "state", lambda n: "stopped"), \
                mock.patch.object(tl, "disk_in_use", lambda n: False), contextlib.redirect_stdout(io.StringIO()):
            tl.reset("n_2")
        m = tl.load("n_2")
        self.assertTrue(m["identity"]); self.assertNotEqual(m["ready_file"], first)          # set up again
        self.assertIn(tpl, self.calls[-1])                                                   # on the template again

    def test_template_gc_build_lock_and_clone_ids(self):
        img = os.path.join(self.tmp, "images")
        os.makedirs(img, exist_ok=True)
        old, new = (os.path.join(img, f"node-generic_alpine-3.2{i}-x86_64-bios-cloudinit-r0-ab-t1.qcow2") for i in (1, 2))
        for f in (old, new):
            open(f, "w").close()
            os.utime(f, (time.time() - 3600, time.time() - 3600))
        os.makedirs(os.path.join(self.tmp, ".bases"))
        base = os.path.join(self.tmp, ".bases", "n1-abcd.qcow2"); open(base, "w").close()   # a clone's source moved here
        backing = {base: old}

        def run(cmd, **k):
            return mock.Mock(returncode=0 if cmd[-1] in backing else 1,
                             stdout=json.dumps({"backing-filename": backing.get(cmd[-1])}))
        with mock.patch.object(tl.subprocess, "run", run):
            tl._gc_node_templates(new)
            self.assertTrue(os.path.exists(old))                                 # still needed by n1 and its clone
            backing.clear(); backing[base] = None
            os.makedirs(os.path.join(self.tmp, "x")); open(os.path.join(self.tmp, "x", "disk.qcow2"), "w").close()
            tl._gc_node_templates(new)                                           # x's disk unreadable: no guess
            self.assertTrue(os.path.exists(old))
            os.remove(os.path.join(self.tmp, "x", "disk.qcow2"))
            tl._gc_node_templates(new)
            self.assertFalse(os.path.exists(old)); self.assertTrue(os.path.exists(new))
        lock = os.path.join(img, "t.lock"); os.mkdir(lock)
        self.assertTrue(tl._builder_alive(lock))                                 # being taken: no pid yet
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write(str(os.getpid()))
        self.assertTrue(tl._builder_alive(lock))
        with open(os.path.join(lock, "pid"), "w") as f:
            f.write("999999999")                                                 # its build was killed
        self.assertFalse(tl._builder_alive(lock))
        m = {"image": "alpine", "user": "user", "ready_file": "/var/lib/terminal-vm/ready-x"}
        tl.node_seed("c2", m, fresh=True)
        script = tl.node_identity_script("c2", m)
        self.assertIn("rm -f /etc/ssh/ssh_host_*", script); self.assertIn("rc-service docker restart", script)
        self.assertNotIn("ssh_host_* /var", tl.node_identity_script("c2", dict(m, fresh_ids=False)))
        self.assertIn("sed -i '/^127[.]0[.]1[.]1", script)                       # the build VM's name replaced

    def test_new_instance_each_seed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            tl.create("n1", None, cpus=1, memory=1, disk=4, image="debian")
        first = self.seed("n1")[2]
        tl.cloud_seed("n1", tl.load("n1"))                                             # clone or reset: set up again
        self.assertNotEqual(first, self.seed("n1")[2])

    def test_refused(self):
        for k in ({"node": True}, {"image": "debian", "os_name": "win11"}, {"image": "debian", "sandbox": True},
                  {"image": "fedora"}):
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                tl.create("x", None, cpus=1, memory=1, disk=4, **k)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))


class ExecCli(unittest.TestCase):
    def test_options_before_dash_dash_program_options_after(self):
        got, tmp = [], tempfile.mkdtemp()
        f = os.path.join(tmp, "in")
        with open(f, "wb") as out:
            out.write(b"data")
        with mock.patch.object(tl, "guest_run", lambda n, argv, t, data: got.append((n, argv, t, data)) or {"exit": 0}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            tl.main(["exec", "n1", "--stdin", f, "--timeout", "5", "--", "docker", "ps", "--format", "{{.Names}}"])
            tl.main(["exec", "n1", "--", "sh", "-c", "exit 3"])
        self.assertEqual(got, [("n1", ["docker", "ps", "--format", "{{.Names}}"], 5.0, b"data"),
                               ("n1", ["sh", "-c", "exit 3"], 60.0, None)])
        self.assertEqual(out.getvalue().splitlines()[0], '{"exit": 0}')
        with self.assertRaisesRegex(SystemExit, "PROGRAM"):
            tl.main(["exec", "n1"])


class CloudImageDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.got = []

    def run_with(self, pages, kind, backing=None, unreadable=False):
        def urlopen(url, timeout=30):
            if url not in pages:
                raise OSError("offline")
            return io.BytesIO(pages[url].encode())

        def download(url, dest, digest, algo):
            self.got.append((url, dest, digest, algo))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "w").close()
        with mock.patch.object(tl, "STORE", self.tmp), mock.patch.object(tl.urllib.request, "urlopen", urlopen), \
                mock.patch.object(tl, "download", download), mock.patch.object(tl.os, "chmod", lambda *a: None), \
                mock.patch.object(tl, "qemu_bin", lambda t: t), \
                mock.patch.object(tl.subprocess, "run", lambda cmd, **k: tl.subprocess.CompletedProcess(
                    cmd, 1 if unreadable else 0, json.dumps({"backing-filename": (backing or {}).get(cmd[-1])}), "")), \
                contextlib.redirect_stderr(io.StringIO()):
            return tl.cloud_image(kind)

    def test_older_unused_images_removed(self):
        idx, img = tl.CLOUD_IMAGES["debian"], os.path.join(self.tmp, "images")
        os.makedirs(img); os.makedirs(os.path.join(self.tmp, "vm1"))
        old_used, old_free = (os.path.join(img, f"debian-13-genericcloud-amd64-{h}.qcow2") for h in ("aaaa", "bbbb"))
        recent = os.path.join(img, "debian-13-genericcloud-amd64-cccc.qcow2")          # handed out to a create now
        for f in (old_used, old_free, recent, os.path.join(img, "debian-live.iso"),
                  os.path.join(self.tmp, "vm1", "disk.qcow2")):
            open(f, "w").close()
            if f != recent:
                os.utime(f, (1, 1))
        sums = {idx + "SHA512SUMS": f"{'cd' * 64}  debian-13-genericcloud-amd64.qcow2\n"}
        uses = {os.path.join(self.tmp, "vm1", "disk.qcow2"): old_used}
        self.run_with(sums, "debian", backing=uses, unreadable=True)               # a disk unreadable: nothing goes
        self.assertIn(os.path.basename(old_free), os.listdir(img))
        os.remove(self.got[-1][1])
        dest = self.run_with(sums, "debian", backing=uses)
        self.assertEqual(sorted(os.listdir(img)),
                         sorted(os.path.basename(f) for f in (dest, old_used, recent, "debian-live.iso")))
        with self.assertRaisesRegex(SystemExit, "not in"):
            self.run_with({idx + "SHA512SUMS": "nothing here\n"}, "debian")

    def test_newest_alpine_verified_by_sha512(self):
        idx, digest = tl.CLOUD_IMAGES["alpine"], "ab" * 64
        listing = "".join(f'<a href="generic_alpine-{v}-x86_64-bios-cloudinit-r0.qcow2">' for v in ("3.9.2", "3.24.1", "3.24.0"))
        dest = self.run_with({idx: listing, idx + "generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2.sha512":
                              digest + "  generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2\n"}, "alpine")
        self.assertEqual(self.got, [(idx + "generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2", dest, digest, "sha512")])
        self.assertTrue(dest.endswith("generic_alpine-3.24.1-x86_64-bios-cloudinit-r0-abababababab.qcow2"))

    def test_debian_and_offline(self):
        idx, digest = tl.CLOUD_IMAGES["debian"], "cd" * 64
        dest = self.run_with({idx + "SHA512SUMS": f"{digest}  debian-13-genericcloud-amd64.qcow2\n"}, "debian")
        self.assertEqual(self.got[0][3], "sha512")
        self.assertTrue(os.path.exists(dest))
        os.remove(dest)
        with self.assertRaisesRegex(SystemExit, "none is cached"):
            self.run_with({}, "debian")
        open(dest, "w").close(); open(dest + ".123.part", "w").close()
        self.assertEqual(self.run_with({}, "debian"), dest)                              # offline: the cached one


class SettledScreenshot(unittest.TestCase):
    def test_waits_for_the_input_to_show(self):
        frames = iter([b"old", b"old", b"new"])  # a loaded guest draws the keystroke late
        class Q:
            def close(self): pass
        old = (tl.qmp, tl.screen_pixels, tl.screenshot)
        tl.qmp, tl.screen_pixels = (lambda n: Q()), (lambda n, q: (next(frames, b"new"), (1, 1)))
        tl.screenshot = lambda n, q=None: (b"png", (1, 1))
        try:
            t = time.time()
            self.assertEqual(tl.settled_screenshot("v", b"old"), b"png")
            self.assertEqual(next(frames, None), None)        # polled until the change
            self.assertLess(time.time() - t, 1.0)             # not the whole timeout
            t = time.time(); tl.settled_screenshot("v", b"new", timeout=0.3)  # no visible effect: bounded
            self.assertLess(time.time() - t, 1.0)
        finally:
            tl.qmp, tl.screen_pixels, tl.screenshot = old


class Rename(unittest.TestCase):
    def test_computer_name(self):
        self.assertEqual(tl.computer_name("web_server.lab-01"), "WEB-SERVER-LAB")
        self.assertEqual(tl.computer_name("w26c"), "W26C")
        self.assertEqual(len(tl.computer_name("a" * 40)), 15)
        self.assertEqual(tl.computer_name("1234"), "VM-1234")      # all digits is invalid
        self.assertNotIn("{{", tl.RENAME_PS.format(name="X"))     # the template formats cleanly


class HostDisk(unittest.TestCase):
    def test_wsl_counts_the_windows_drive(self):
        from unittest import mock
        usage = lambda p: mock.Mock(free=10 if p == "/mnt/c" else 500)
        with mock.patch.object(tl.shutil, "disk_usage", usage), mock.patch.object(tl, "in_wsl", lambda: True), \
                mock.patch.object(tl.os.path, "isdir", lambda p: True):
            self.assertEqual(tl.host_free_bytes("/store"), 10)
        with mock.patch.object(tl.shutil, "disk_usage", usage), mock.patch.object(tl, "in_wsl", lambda: False):
            self.assertEqual(tl.host_free_bytes("/store"), 500)


@unittest.skipUnless(os.name == "nt", "Winsock AF_UNIX: Windows hosts only")
class WindowsUnixSocket(unittest.TestCase):
    """The Windows-host path: QMP over a unix socket through Winsock (Python has no AF_UNIX there)."""
    def test_qmp_over_winsock_unix(self):
        import ctypes
        ws = ctypes.WinDLL("ws2_32")
        ws.socket.restype = ws.accept.restype = ctypes.c_size_t
        ws.bind.argtypes = ws.connect.argtypes = (ctypes.c_size_t, ctypes.c_char_p, ctypes.c_int)
        ws.listen.argtypes, ws.accept.argtypes = (ctypes.c_size_t, ctypes.c_int), (ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
        tmp = tempfile.mkdtemp(); tl.STORE = tmp
        os.makedirs(os.path.join(tmp, "t"))
        path = tl.endpoint("t", "qmp")
        srv = ws.socket(1, 1, 0); addr = struct.pack("H108s", 1, path.encode())
        self.assertEqual(ws.bind(srv, addr, len(addr)), 0); self.assertEqual(ws.listen(srv, 1), 0)
        def serve():
            c = socket.socket(fileno=ws.accept(srv, None, None))
            with c, c.makefile("rwb") as f:
                f.write(b'{"QMP": {}}\n'); f.flush()
                for line in f:
                    f.write(b'{"return": {"status": "paused"}}\n'); f.flush()
        threading.Thread(target=serve, daemon=True).start()
        self.assertEqual(tl.state("t"), "paused")
        self.assertEqual(tl.state("nope"), "stopped")       # missing socket: a clean "stopped", not a crash


if __name__ == "__main__":
    unittest.main()
