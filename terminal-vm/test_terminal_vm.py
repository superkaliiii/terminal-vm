import contextlib, io, json, os, re, socket, struct, sys, tempfile, threading, unittest, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import terminal_vm as tl

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
        self.assertIn("64G", calls[-1][0])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "w11", "answers", "autounattend.xml")))
        with open(os.path.join(self.tmp, "w11", "answers", "virtio-win-guest-tools.exe"), "rb") as f:
            self.assertEqual(f.read(), b"MZ fake")   # the guest agent's installer rides on the answers drive
        with open(os.path.join(self.tmp, "w11", "answers", "autounattend.xml"), encoding="utf-8") as f:
            xml = f.read()
        self.assertLess(xml.index("virtio-win-guest-tools.exe /install /quiet"), xml.index("shutdown /l"))

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
        def run(argv, **k):
            self.calls.append(argv)
            if argv[1] == "create":
                open([a for a in argv if a.endswith((".qcow2", ".new"))][-1], "w").close()  # the image created
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

    def test_clone_protects_its_base(self):
        self.quiet(tl.clone, "v", "c")
        create = next(c for c in self.calls if c[1] == "create")
        self.assertEqual(create[2:7], ["-f", "qcow2", "-F", "qcow2", "-b"])
        self.assertEqual(create[7], os.path.join(self.d, "disk.qcow2"))
        self.assertEqual(tl.load("c")["base"], "v")
        for f, *a in ((tl.rm, "v"), (tl.reset, "v"), (tl.snapshot, "v", "save", "x"), (tl.start, "v")):
            with self.assertRaises(SystemExit):  # writing the base would corrupt the clone
                f(*a)
        self.quiet(tl.rm, "c")
        self.quiet(tl.rm, "v")

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
        cpu, rss = tl._proc_times(os.getpid())          # this test process, on Linux and Windows alike
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

    def test_agent_authority(self):
        """Agents run sandboxed VMs and their own; the user's VMs are off-limits; risky steps ask the user."""
        asked = []
        def yes(msg, cli): asked.append(cli)
        def no(msg, cli): raise RuntimeError("declined: " + cli)
        with self.assertRaises(SystemExit):
            tl.agent_call("vm_start", {"vm": "v"}, yes)                       # the user's VM
        with self.assertRaises(SystemExit):
            tl.agent_call("vm_create", {"vm": "x", "iso": "../../etc/passwd"}, yes)   # store images only
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
        with open(os.path.join(self.tmp, "agent.log"), encoding="utf-8") as f:
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
