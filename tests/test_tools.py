"""Command-line tools against the synthetic doorbell."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from tests.fake_device import FakeDoorbell  # noqa: E402


class Tools(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDoorbell(echo=True).start()
        self.env = dict(os.environ, GLNK_HOST="127.0.0.1", GLNK_PORT=str(self.dev.port),
                        GLNK_PASSWORD="1234", GLNK_UNLOCK_CODE="9876")

    def tearDown(self):
        self.dev.stop()

    def test_latency_detects_the_echo(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "latency.py"), "--runs", "2"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        delays = [float(x) for x in re.findall(r"heard after ([\d.]+) s", r.stdout)]
        self.assertEqual(len(delays), 2, r.stdout)
        self.assertTrue(all(0 < d < 1 for d in delays), delays)

    def tool(self, *args, stdin=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "glnk_tool.py"),
                               "--host", "127.0.0.1", "--port", str(self.dev.port), *args],
                              env=self.env, input=stdin, capture_output=True, text=True, timeout=60)

    def test_record(self):
        with tempfile.TemporaryDirectory() as out:
            r = self.tool("record", "--seconds", "1", "--out", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertGreater(os.path.getsize(os.path.join(out, "video.h264")), 0)
            self.assertEqual(os.path.getsize(os.path.join(out, "audio.alaw")) % 320, 0)

    def test_chime(self):
        r = self.tool("chime")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(len(self.dev.talk_audio), 8000)

    def test_unlock_needs_confirmation(self):
        r = self.tool("unlock", stdin="no\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("unlock ok", self.dev.events)
        r = self.tool("unlock", stdin="YES\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("door opened", r.stdout)


if __name__ == "__main__":
    unittest.main()
