import json, os, socket, struct, sys, tempfile, threading, unittest, zlib
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
        self.status = "running"
        def reply(m):
            if m["execute"] == "screendump":
                with open(m["arguments"]["filename"], "wb") as f:
                    f.write(tiny_png(640, 480))
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


if __name__ == "__main__":
    unittest.main()
