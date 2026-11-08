#!/usr/bin/env python3
"""
Measure the doorbell's own audio round trip, without any browser or go2rtc:
send a short 1 kHz beep to the speaker (talk) and time when the doorbell's microphone hears it
(visitor audio). Standard library only.

  GLNK_HOST=<doorbell-ip> GLNK_PASSWORD=... python3 tools/latency.py [--runs 3]

Inside the Frigate container (package mounted on /glnk):
  docker exec -i frigate sh -c 'PYTHONPATH=/glnk python3 - --runs 3' < tools/latency.py
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "glnk_bridge"))
sys.path.insert(0, "/glnk")
from glnk import GlnkClient, alaw_encode_sample  # noqa: E402

RATE, FRAME = 8000, 320          # 40 ms frames


def alaw_decode(b):
    b ^= 0x55
    t = (b & 0x0F) << 4
    seg = (b & 0x70) >> 4
    t = t + 8 if seg == 0 else (t + 0x108) << (seg - 1)
    return t if b & 0x80 else -t


DECODE = [alaw_decode(i) for i in range(256)]


def tone_level(frame, freq=1000.0):
    """Goertzel magnitude of `freq` in one frame (normalised)."""
    k = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = 0.0
    for byte in frame:
        s0 = DECODE[byte] + k * s1 - s2
        s2, s1 = s1, s0
    return math.sqrt(max(s1 * s1 + s2 * s2 - k * s1 * s2, 0.0)) / len(frame)


def frames(seconds, freq=None, amp=6000):
    n = int(seconds * RATE)
    pcm = [int(amp * math.sin(2 * math.pi * freq * i / RATE)) if freq else 0 for i in range(n)]
    data = bytes(alaw_encode_sample(v) for v in pcm)
    return [data[i:i + FRAME] for i in range(0, len(data) - FRAME + 1, FRAME)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=6.0)
    a = ap.parse_args()

    heard = []                                   # (time, level) of each received audio frame
    c = GlnkClient(os.environ["GLNK_HOST"], int(os.environ.get("GLNK_PORT", "59613")),
                   os.environ.get("GLNK_USER", "admin"), os.environ["GLNK_PASSWORD"],
                   on_audio=lambda d: heard.append((time.monotonic(), tone_level(d))))
    c.connect()
    try:
        if not c.talk_start():
            print("talk not acknowledged")
        silence, beep = frames(0.04), frames(0.2, 1000)

        def send_realtime(seq, seconds=None):
            """Send frames at real-time pace (40 ms each), pumping the receive side in between."""
            t = time.monotonic()
            i = 0
            while True:
                if seconds is None and i >= len(seq):
                    return
                if seconds is not None and time.monotonic() - t >= seconds:
                    return
                c.talk_send(seq[i % len(seq)])
                i += 1
                while time.monotonic() < t + i * 0.04:
                    c.pump(0.005)

        send_realtime(silence, 2.0)              # settle, measure background level
        base = sorted(l for _, l in heard[-40:])
        floor = base[len(base) // 2] if base else 0.0
        results = []
        for run in range(a.runs):
            heard.clear()
            t0 = time.monotonic()
            send_realtime(beep)
            hit = None
            end = t0 + a.timeout
            while time.monotonic() < end and hit is None:
                send_realtime(silence, 0.04)
                for t, lvl in heard:
                    if lvl > max(floor * 6, 40):
                        hit = t
                        break
            if hit:
                results.append(hit - t0)
                print("run %d: beep heard after %.2f s" % (run + 1, hit - t0))
            else:
                print("run %d: beep not detected (floor %.1f, max %.1f)"
                      % (run + 1, floor, max((l for _, l in heard), default=0)))
            send_realtime(silence, 1.5)
        if results:
            print("doorbell round trip (speaker -> mic, GLNK only): %.2f s" % (sum(results) / len(results)))
        c.talk_stop()
        c.pump(0.1)
    finally:
        c.close()


if __name__ == "__main__":
    main()
