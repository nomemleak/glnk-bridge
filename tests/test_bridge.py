"""Bridge service: real FTP uploads (ftplib) -> ring event, picture, debounce; door unlock via MQTT action."""
import ftplib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from glnk_bridge.bridge import Bridge, Config
except ImportError as e:                       # paho-mqtt / pyftpdlib not installed
    raise unittest.SkipTest("bridge dependencies missing: %s" % e)
from tests.fake_device import FakeDoorbell  # noqa: E402

logging.getLogger("pyftpdlib").addHandler(logging.NullHandler())   # keep test output quiet
logging.getLogger("glnk-bridge").addHandler(logging.NullHandler())

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 200 + b"\xff\xd9"


class FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.published.append((topic, payload, retain))
        return mock.Mock()

    def topics(self, suffix):
        return [p for t, p, _ in self.published if t.endswith(suffix)]


class BridgeTest(unittest.TestCase):
    def make(self, **env):
        self.tmp = tempfile.TemporaryDirectory()
        base = {"GLNK_HOST": "127.0.0.1", "GLNK_PASSWORD": "1234", "GLNK_UNLOCK_CODE": "9876",
                "FTP_PASSWORD": "ftp-secret", "FTP_USER": "doorbell", "FTP_PORT": "0",
                "FTP_BIND": "127.0.0.1", "FTP_ALLOWED_IP": "127.0.0.1", "DATA_DIR": self.tmp.name,
                "DEVICE_ID": "test", "RING_GAP": "1", "RING_HOLD": "0.3"}
        base.update(env)
        with mock.patch.dict(os.environ, base, clear=False):
            self.bridge = Bridge(Config())
        self.mqtt = self.bridge.mqtt = FakeMqtt()
        return self.bridge

    def start_ftp(self):
        self.server = self.bridge.ftp_server()
        self.port = self.server.socket.getsockname()[1]
        self.stop = threading.Event()

        def loop():
            while not self.stop.is_set():
                self.server.serve_forever(timeout=0.05, blocking=False, handle_exit=False)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def tearDown(self):
        if getattr(self, "server", None):
            self.stop.set()
            self.thread.join(2)
            self.server.close_all()
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def upload(self, names, user="doorbell", password="ftp-secret"):
        ftp = ftplib.FTP()
        ftp.connect("127.0.0.1", self.port, timeout=5)
        ftp.login(user, password)
        for d in ("2026-01-01", "IPC000000", "1"):         # same layout as the device
            try:
                ftp.mkd(d)
            except ftplib.error_perm:
                pass
            ftp.cwd(d)
        for n in names:
            ftp.storbinary("STOR " + n, io.BytesIO(JPEG))
        ftp.quit()

    def wait(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_burst_of_pictures_is_one_ring(self):
        self.make()
        self.start_ftp()
        self.upload(["10_00_00.jpg", "10_00_01.jpg", "10_00_02.jpg"])
        self.assertTrue(self.wait(lambda: len(self.mqtt.topics("/snapshot")) == 3))
        events = self.mqtt.topics("/event")
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]), {"event_type": "ring"})
        self.assertEqual(self.mqtt.topics("/snapshot")[-1], JPEG)
        self.assertEqual(self.mqtt.topics("/ringing")[0], "ON")
        self.assertTrue(self.wait(lambda: self.mqtt.topics("/ringing")[-1] == "OFF"))
        with open(os.path.join(self.tmp.name, "last.jpg"), "rb") as f:
            self.assertEqual(f.read(), JPEG)

    def test_new_ring_after_gap(self):
        self.make(RING_GAP="0.3")
        self.start_ftp()
        self.upload(["a.jpg"])
        time.sleep(0.5)
        self.upload(["b.jpg"])
        self.assertTrue(self.wait(lambda: len(self.mqtt.topics("/event")) == 2))

    def test_non_jpeg_upload_is_ignored(self):
        self.make()
        self.start_ftp()
        self.upload(["notes.txt"])
        time.sleep(0.3)
        self.assertEqual(self.mqtt.topics("/event"), [])

    def test_wrong_password_and_other_ip_are_refused(self):
        self.make()
        self.start_ftp()
        with self.assertLogs("glnk-bridge", level="WARNING") as logs:
            with self.assertRaises(ftplib.error_perm):
                self.upload(["x.jpg"], password="nope")
        self.assertIn("FTP login failed for user 'doorbell'", logs.output[0])
        self.tearDown()
        self.make(FTP_ALLOWED_IP="10.255.255.1")
        self.start_ftp()
        with self.assertRaises((ftplib.Error, EOFError, OSError)):
            self.upload(["x.jpg"])
        self.assertEqual(self.mqtt.topics("/event"), [])

    def test_discovery_entities(self):
        self.make()
        self.bridge._discover()
        configs = {t: json.loads(p) for t, p, _ in self.mqtt.published if t.endswith("/config")}
        kinds = sorted(t.split("/")[1] for t in configs)
        self.assertEqual(kinds, ["binary_sensor", "button", "button", "button", "event"] + ["image"] * 5
                         + ["sensor"] * 6)                    # + 4 history pictures and their times
        ring = next(c for t, c in configs.items() if "/event/" in t)
        self.assertEqual(ring["device_class"], "doorbell")

    def history_times(self):
        return [p for t, p, _ in self.mqtt.published if "/history/" in t and t.endswith("/time")]

    def test_history_keeps_one_entry_per_ring_newest_first(self):
        self.make(RING_GAP="0.3")
        self.start_ftp()
        self.upload(["a1.jpg", "a2.jpg"])                  # one ring, two pictures
        time.sleep(0.5)
        self.upload(["b1.jpg"])                            # second ring
        self.assertTrue(self.wait(lambda: len(self.bridge.history) == 2 and
                                  len(self.mqtt.topics("/event")) == 2))
        h = self.bridge.history
        self.assertGreaterEqual(h[0]["time"], h[1]["time"])
        self.assertEqual(self.mqtt.topics("/history/1/image")[-1], JPEG)
        self.assertEqual(self.mqtt.topics("/history/1/time")[-1], h[0]["time"])
        self.assertEqual(self.mqtt.topics("/history/2/time")[-1], h[1]["time"])
        self.assertEqual(self.mqtt.topics("/history/3/time")[-1], "None")

    def test_history_is_trimmed_and_survives_a_restart(self):
        self.make(RING_GAP="0", HISTORY_SIZE="2")
        pic = os.path.join(self.tmp.name, "p.jpg")
        with open(pic, "wb") as f:
            f.write(JPEG)
        for n in range(3):
            self.bridge.last_ring = 0.0
            with mock.patch("glnk_bridge.bridge.datetime") as dt:
                from datetime import datetime as real, timezone as tz
                dt.now.return_value = real(2026, 1, 1, 10, 0, n, tzinfo=tz.utc)
                self.bridge.on_snapshot(pic)
        self.assertEqual([h["time"] for h in self.bridge.history],
                         ["2026-01-01T10:00:02+00:00", "2026-01-01T10:00:01+00:00"])
        files = sorted(f for f in os.listdir(os.path.join(self.tmp.name, "history")) if f.endswith(".jpg"))
        self.assertEqual(files, ["ring_20260101_100001.jpg", "ring_20260101_100002.jpg"])
        with mock.patch.dict(os.environ, {"GLNK_HOST": "127.0.0.1", "GLNK_PASSWORD": "x", "FTP_PASSWORD": "x",
                                          "DATA_DIR": self.tmp.name, "HISTORY_SIZE": "2"}):
            again = Bridge(Config())
        self.assertEqual(again.history, self.bridge.history)

    def test_history_rebuilt_from_existing_ftp_pictures(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = os.path.join(tmp.name, "ftp", "2026-01-01", "IPC000000", "1")
        os.makedirs(d)
        base = 1767261600                                  # 2026-01-01T10:00:00Z
        for name, offset in [("a1.jpg", 0), ("a2.jpg", 2), ("b1.jpg", 100), ("c1.jpg", 300),
                             ("c2.jpg", 301), ("notes.txt", 400)]:
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(name.encode())
            os.utime(p, (base + offset, base + offset))
        env = {"GLNK_HOST": "127.0.0.1", "GLNK_PASSWORD": "x", "FTP_PASSWORD": "x",
               "DATA_DIR": tmp.name, "HISTORY_SIZE": "2", "RING_GAP": "20"}
        with mock.patch.dict(os.environ, env):
            b = Bridge(Config())
        self.assertEqual([h["time"] for h in b.history],
                         ["2026-01-01T10:05:00+00:00", "2026-01-01T10:01:40+00:00"])   # c, then b
        with open(os.path.join(tmp.name, "history", b.history[0]["file"]), "rb") as f:
            self.assertEqual(f.read(), b"c2.jpg")          # last picture of the ring
        with mock.patch.dict(os.environ, env):
            again = Bridge(Config())                       # no second import once history exists
        self.assertEqual(again.history, b.history)

    def test_simulated_ring_is_not_a_visitor(self):
        self.make()
        self.bridge.simulate_ring()
        self.assertEqual(self.bridge.history, [])
        self.assertEqual(len(self.mqtt.topics("/event")), 1)

    def test_history_disabled(self):
        self.make(HISTORY_SIZE="0")
        self.bridge._discover()
        self.assertFalse([t for t, _, _ in self.mqtt.published if "visitor_1" in t])

    def test_unlock_through_the_device(self):
        dev = FakeDoorbell(unlock_code="9876").start()
        try:
            self.make(GLNK_PORT=str(dev.port))
            self.bridge.unlock()
            state = self.mqtt.topics("/unlock/state")[-1]
            self.assertTrue(state.startswith("opened"), state)
            self.make(GLNK_PORT=str(dev.port), GLNK_UNLOCK_CODE="0000")
            self.bridge.unlock()
            self.assertTrue(self.mqtt.topics("/unlock/state")[-1].startswith("refused"))
        finally:
            dev.stop()

    def test_unlock_without_code(self):
        self.make(GLNK_UNLOCK_CODE="")
        self.bridge.unlock()
        self.assertIn("no unlock code", self.mqtt.topics("/unlock/state")[-1])


if __name__ == "__main__":
    unittest.main()
