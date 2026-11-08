"""go2rtc helper (av.py), run as a subprocess exactly like go2rtc does."""
import os
import signal
import subprocess
import sys
import time
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from tests.fake_device import FakeDoorbell, tone  # noqa: E402

AV = os.path.join(ROOT, "glnk_bridge", "av.py")


class AvHelper(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDoorbell().start()
        self.env = dict(os.environ, GLNK_HOST="127.0.0.1", GLNK_PORT=str(self.dev.port),
                        GLNK_PASSWORD="1234")

    def tearDown(self):
        self.dev.stop()

    def run_av(self, *args, **kw):
        return subprocess.Popen([sys.executable, AV, *args], env=self.env, stderr=subprocess.PIPE, **kw)

    def test_listen_pipe_writes_wav_then_audio(self):
        p = self.run_av("listen", stdout=subprocess.PIPE)
        try:
            header = p.stdout.read(46)
            self.assertEqual(header[:4], b"RIFF")
            self.assertEqual(header[8:16], b"WAVEfmt ")
            self.assertEqual(header[20:22], b"\x06\x00")           # format 6 = A-law
            self.assertEqual(header[38:42], b"data")
            audio = p.stdout.read(320 * 10)
            self.assertEqual(len(audio), 3200)
        finally:
            p.send_signal(signal.SIGTERM)
            p.wait(5)
        err = p.stderr.read().decode()
        self.assertIn("stop requested", err)
        self.assertIn("session closed", err)
        self.assertTrue(self.dev.wait_for("bye"))

    def test_talk_until_stdin_closes(self):
        audio = tone(seconds=0.8)
        p = self.run_av("talk", stdin=subprocess.PIPE)
        p.stdin.write(audio)
        p.stdin.close()
        p.wait(10)
        self.assertTrue(self.dev.wait_for("talk stop"))
        self.assertTrue(self.dev.wait_for("bye"))
        self.assertEqual(bytes(self.dev.talk_audio), audio)

    def test_talk_sigterm_closes_the_session(self):
        """go2rtc stops a backchannel with killsignal=15: the device must not keep a stale talk."""
        p = self.run_av("talk", stdin=subprocess.PIPE)
        self.assertTrue(self.dev.wait_for("talk start", 5))
        p.stdin.write(tone(seconds=0.4))
        p.stdin.flush()
        time.sleep(0.3)
        t = time.monotonic()
        p.send_signal(signal.SIGTERM)
        p.wait(5)
        self.assertLess(time.monotonic() - t, 2)
        for event in ("talk stop", "bye", "shutdown"):
            self.assertTrue(self.dev.wait_for(event), event)

    def test_missing_settings(self):
        env = dict(os.environ)
        env.pop("GLNK_HOST", None)
        env.pop("GLNK_PASSWORD", None)
        r = subprocess.run([sys.executable, AV, "talk"], env=env, capture_output=True, timeout=10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"GLNK_HOST", r.stderr)


if __name__ == "__main__":
    unittest.main()
