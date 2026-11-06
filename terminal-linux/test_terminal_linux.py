import contextlib, io, json, os, socket, struct, sys, tempfile, threading, unittest, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import terminal_linux as tl

POSIX = os.name == "posix" and hasattr(socket, "AF_UNIX")


def tiny_png(w, h):
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\0" + b"\0\0\0" * w for _ in range(h))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


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
                with open(m["arguments"]["filename"], "wb") as f:
                    f.write(tiny_png(640, 480))
            if m["execute"] == "human-monitor-command":
                return json.dumps({"return": self.hmp}).encode() + b"\n"
            return b'{"event": "X"}\n' + json.dumps({"return": {"status": self.status}}).encode() + b"\n"
        self.q = FakeServer(os.path.join(d, "qmp.sock"), b'{"QMP": {}}\n', reply); self.q.start()

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
            self.assertIn("<AdministratorPassword><Value>p&lt;&amp;&gt;w-Aa1</Value>", xml)  # Server OOBE stops without it
            spec = root.find(f"{ns}settings[@pass='specialize']")  # default profile, so the first session has it
            self.assertIn(r"HKU\TLDefault", ET.tostring(spec, encoding="unicode"))
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
        try:
            with self.assertRaises(SystemExit):  # Windows 8 and XP need the user's key
                tl.create("w8", iso, os_name="win8")
            tl.create("w11", iso, os_name="win11", layout="fr")
        finally:
            tl.subprocess.run, tl.qemu_bin = old
        with open(os.path.join(self.tmp, "w11", "machine.json")) as f:
            m = json.load(f)
        self.assertEqual((m["os"], m["memory_mb"], m["user"]), ("win11", 4096, "user"))
        self.assertTrue(m["password"].endswith("-Aa1") and len(m["password"]) == 12)
        self.assertIn("64G", calls[-1][0])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "w11", "answers", "autounattend.xml")))


class StoppedVm(unittest.TestCase):
    """rm, reset, display and offline snapshots: no QEMU running, qemu-img faked."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); tl.STORE = self.tmp
        self.d = os.path.join(self.tmp, "v"); os.makedirs(self.d)
        with open(os.path.join(self.d, "machine.json"), "w") as f:
            json.dump({"cpus": 1, "memory_mb": 1024, "width": 1280, "height": 800}, f)
        with open(os.path.join(self.d, "disk.qcow2"), "w") as f: f.write("old")
        self.calls, self.old = [], (tl.subprocess.run, tl.qemu_bin)
        def run(argv, **k):
            self.calls.append(argv)
            if argv[1] == "create":
                open(argv[4], "w").close()
            out = '{"virtual-size": 68719476736}' if argv[1] == "info" else \
                "ID TAG VM SIZE DATE\n1 clean 0 B 2026-11-06 10:00:00\n" if argv[1:3] == ["snapshot", "-l"] else ""
            return tl.subprocess.CompletedProcess(argv, 0, out, "")
        tl.subprocess.run, tl.qemu_bin = run, (lambda t: t)

    def tearDown(self):
        tl.subprocess.run, tl.qemu_bin = self.old

    def quiet(self, f, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            f(*a)
        return json.loads(buf.getvalue())

    def test_reset_keeps_size_and_config(self):
        self.quiet(tl.reset, "v")
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
