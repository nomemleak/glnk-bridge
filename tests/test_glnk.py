import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from glnk_bridge.glnk import (UDT, GlnkClient, GlnkError, alaw_encode_sample, chime,  # noqa: E402
                              login_payload, owsp_frame, owsp_pop, seq_add, seq_diff)
from tests.fake_device import FakeDoorbell, tone  # noqa: E402


class Codec(unittest.TestCase):
    def test_owsp_roundtrip_and_partial(self):
        f1 = owsp_frame([(40, struct.pack("<I", 5)), (41, b"x" * 68)])
        f2 = owsp_frame([(5005, b"")])
        buf = bytearray(f1 + f2[:5])
        frames = owsp_pop(buf)
        self.assertEqual(frames, [[(40, struct.pack("<I", 5)), (41, b"x" * 68)]])
        buf += f2[5:]
        self.assertEqual(owsp_pop(buf), [[(5005, b"")]])
        self.assertEqual(buf, bytearray())

    def test_owsp_desync_is_detected(self):
        with self.assertRaises(GlnkError):
            owsp_pop(bytearray(b"\xff\xff\xff\xff\0\0\0\0"))

    def test_login_payload_layout(self):
        v = login_payload("admin", "1234")
        self.assertEqual(len(v), 68)
        self.assertEqual(v[1:6], b"admin")
        self.assertEqual(v[33:37], b"1234")
        self.assertEqual(v[60:64], struct.pack("<I", 1))
        self.assertEqual((v[64], v[65]), (1, 2))

    def test_sequence_wraparound(self):
        self.assertEqual(seq_add(0x7FFFFFFF), 0)
        self.assertEqual(seq_diff(0, 0x7FFFFFFF), 1)
        self.assertEqual(seq_diff(0x7FFFFFFF, 0), -1)

    def test_alaw(self):
        self.assertEqual(alaw_encode_sample(0), 0xD5)
        self.assertEqual(len(chime()) % 320, 0)


class Session(unittest.TestCase):
    def setUp(self):
        self.dev = None

    def tearDown(self):
        if self.dev:
            self.dev.stop()

    def client(self, password="1234", **kw):
        return GlnkClient("127.0.0.1", self.dev.port, "admin", password, **kw)

    def test_login_ok_and_device_info(self):
        self.dev = FakeDoorbell().start()
        c = self.client()
        try:
            info = c.connect()
            self.assertIn("IPC01", info)
        finally:
            c.close()
        self.assertTrue(self.dev.wait_for("bye"))
        self.assertTrue(self.dev.wait_for("shutdown"))

    def test_login_refused(self):
        self.dev = FakeDoorbell().start()
        c = self.client(password="wrong")
        try:
            with self.assertRaisesRegex(GlnkError, "code 7"):
                c.connect()
        finally:
            c.close()

    def test_no_device(self):
        udt = UDT("127.0.0.1", 9)                          # discard port: nothing answers
        try:
            with self.assertRaisesRegex(GlnkError, "no answer"):
                udt.connect(timeout=1.0)
        finally:
            udt.close()

    def test_unlock(self):
        self.dev = FakeDoorbell(unlock_code="9876").start()
        c = self.client()
        try:
            c.connect()
            self.assertFalse(c.unlock("0000"))
            self.assertTrue(c.unlock("9876", delay=3))
        finally:
            c.close()
        self.assertEqual(self.dev.events.count("unlock ok"), 1)
        self.assertEqual(self.dev.unlocks[-1], 3)

    def test_talk_audio_reaches_the_speaker(self):
        self.dev = FakeDoorbell().start()
        c = self.client()
        audio = tone(seconds=1.0)
        try:
            c.connect()
            self.assertTrue(c.talk_start())
            for n in range(0, len(audio), 320):
                c.talk_send(audio[n:n + 320])
                c.pump(0.005)
            c.talk_stop()
            c.pump(0.3)
        finally:
            c.close()
        self.assertTrue(self.dev.wait_for("talk stop"))
        self.assertEqual(bytes(self.dev.talk_audio), audio)

    def test_stream_survives_packet_loss(self):
        self.dev = FakeDoorbell(loss=0.1, video_size=5000).start()   # 4 packets per video frame
        seen = []
        audio = []
        c = self.client(on_video=lambda d, key: seen.append(struct.unpack(">I", d[5:9])[0]),
                        on_audio=audio.append)
        try:
            c.connect()
            end = time.monotonic() + 2.5
            while time.monotonic() < end:
                c.pump(0.05)
        finally:
            c.close()
        self.assertGreater(self.dev.dropped, 0)
        self.assertGreater(len(seen), 10)
        self.assertEqual(seen, list(range(len(seen))))      # every frame, in order, none missing
        self.assertTrue(all(len(a) == 320 for a in audio))


if __name__ == "__main__":
    unittest.main()
