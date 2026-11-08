#!/usr/bin/env python3
"""
glnk_tool.py - command-line test client for GLNK (Goolink / ControlCam2) doorbells, LAN only.

  info     connect, log in and print the device information
  record   record N seconds of video (H.264) and visitor audio (G.711 A-law)
  chime    play a short chime on the doorbell speaker
  unlock   open the door (asks for confirmation)

Secrets come from GLNK_PASSWORD / GLNK_UNLOCK_CODE or are prompted, never passed as arguments.

  python3 tools/glnk_tool.py --host 192.168.1.50 info
  python3 tools/glnk_tool.py --host 192.168.1.50 record --seconds 10 --out capture
"""
import argparse
import getpass
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from glnk_bridge.glnk import GlnkClient, chime  # noqa: E402


def secret(env, prompt):
    return os.environ.get(env) or getpass.getpass(prompt)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, help="doorbell IP")
    ap.add_argument("--port", type=int, default=59613)
    ap.add_argument("--user", default="admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    rec = sub.add_parser("record")
    rec.add_argument("--seconds", type=float, default=10)
    rec.add_argument("--out", default="glnk_capture")
    sub.add_parser("chime")
    sub.add_parser("unlock")
    a = ap.parse_args()

    password = secret("GLNK_PASSWORD", "Device password: ")
    code = None
    if a.cmd == "unlock":
        code = secret("GLNK_UNLOCK_CODE", "Unlock code: ")
        if input("Really OPEN THE DOOR? Type YES: ").strip() != "YES":
            sys.exit("cancelled")

    video = audio = None
    counts = {"video": 0, "audio": 0}
    if a.cmd == "record":
        os.makedirs(a.out, exist_ok=True)
        video = open(os.path.join(a.out, "video.h264"), "wb")
        audio = open(os.path.join(a.out, "audio.alaw"), "wb")

    def on_video(data, keyframe):
        counts["video"] += 1
        if video:
            video.write(data)

    def on_audio(data):
        counts["audio"] += 1
        if audio:
            audio.write(data)

    c = GlnkClient(a.host, a.port, a.user, password, on_video=on_video, on_audio=on_audio)
    try:
        info = c.connect()
        print("logged in:", " | ".join(info) or "(no device info)")
        if a.cmd == "record":
            end = time.monotonic() + a.seconds
            while time.monotonic() < end:
                c.pump(0.1)
            print("received %d video frames, %d audio frames (%.1f s of audio)"
                  % (counts["video"], counts["audio"], counts["audio"] * 0.04))
            print("to watch: ffmpeg -f h264 -i %s/video.h264 -f alaw -ar 8000 -ac 1 -i %s/audio.alaw "
                  "-c:v copy -c:a aac %s/capture.mp4" % (a.out, a.out, a.out))
        elif a.cmd == "chime":
            print("talk acknowledged:", c.talk_start())
            data = chime()
            start = time.monotonic()
            for n in range(0, len(data), 320):
                c.talk_send(data[n:n + 320])
                c.pump(max(0.0, start + (n // 320 + 1) * 0.04 - time.monotonic()))
            c.talk_stop()
            c.pump(0.2)
            print("chime sent")
        elif a.cmd == "unlock":
            print("door opened" if c.unlock(code) else "refused by the device")
    finally:
        c.close()
        for f in (video, audio):
            if f:
                f.close()


if __name__ == "__main__":
    main()
