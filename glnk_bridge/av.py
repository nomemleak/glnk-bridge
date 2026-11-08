#!/usr/bin/env python3
"""
Audio helper for go2rtc (two-way audio with a GLNK doorbell). Standard library only, so it can
run inside the Frigate container (go2rtc `exec` sources); ffmpeg is only needed for `listen OUTPUT`.

  listen  : visitor audio (G.711 A-law 8 kHz) -> WAV on stdout, read directly by go2rtc (lowest latency,
            no ffmpeg):  exec:python3 /glnk/av.py listen#killsignal=15#killtimeout=3
            or, with an output (RTSP URL from go2rtc or a file), through ffmpeg:
                         exec:python3 /glnk/av.py listen {output}#killsignal=15#killtimeout=3
  talk    : A-law 8 kHz on stdin (go2rtc backchannel) -> doorbell speaker
            go2rtc:  exec:python3 /glnk/av.py talk#backchannel=1#audio=alaw/8000#killsignal=15#killtimeout=3

Connection settings come from the environment: GLNK_HOST, GLNK_PORT, GLNK_USER, GLNK_PASSWORD.
"""
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from glnk import GlnkClient, GlnkError  # noqa: E402

FRAME = 320  # 40 ms of A-law at 8 kHz


class Stop(Exception):
    """Raised by SIGTERM/SIGINT so the session is closed cleanly (talk stop + bye + UDT shutdown).
    Without it the doorbell keeps a stale session and ignores the voice of the next call."""


def _on_signal(signum, frame):
    raise Stop()


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def log(*a):
    print("[glnk-av]", *a, file=sys.stderr, flush=True)


def find_ffmpeg():
    for c in (os.environ.get("FFMPEG"), shutil.which("ffmpeg"), "/usr/lib/ffmpeg/7.0/bin/ffmpeg",
              "/usr/lib/btbn-ffmpeg/bin/ffmpeg", "/usr/lib/ffmpeg/6.0/bin/ffmpeg"):
        if c and os.path.exists(c):
            return c
    sys.exit("ffmpeg not found (set FFMPEG=/path/to/ffmpeg)")


def client(**kw):
    missing = [v for v in ("GLNK_HOST", "GLNK_PASSWORD") if not os.environ.get(v)]
    if missing:
        sys.exit("missing environment variable(s): " + ", ".join(missing))
    return GlnkClient(os.environ["GLNK_HOST"], int(os.environ.get("GLNK_PORT", "59613")),
                      os.environ.get("GLNK_USER", "admin"), os.environ["GLNK_PASSWORD"], **kw)


def wav_header_alaw(rate=8000):
    """Streaming WAV header (unknown length) for mono G.711 A-law, as go2rtc's pipe reader expects."""
    fmt = struct.pack("<HHIIHHH", 6, 1, rate, rate, 1, 8, 0)
    return b"RIFF\xff\xff\xff\xffWAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data\xff\xff\xff\xff"


def listen_pipe():
    """Visitor audio -> WAV on stdout (go2rtc exec pipe). Each 40 ms frame is written as it arrives."""
    out = sys.stdout.buffer
    out.write(wav_header_alaw())            # before connecting: go2rtc sees the stream as ready at once
    out.flush()

    def on_audio(data):
        out.write(data)
        out.flush()

    c = client(on_audio=on_audio)
    try:
        c.connect()
        log("listening (pipe)")
        while not c.udt.closed:
            c.pump(0.02)
    except (BrokenPipeError, GlnkError, OSError) as e:
        log("stopped:", e)
    except Stop:
        log("stop requested")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        c.close()
        log("session closed")


def listen(output):
    """Visitor audio -> ffmpeg -> output (RTSP URL from go2rtc, or any ffmpeg output for tests)."""
    # Low latency: no input probing (raw A-law needs none; the default 5 s analysis delayed the
    # RTSP publish and made go2rtc consumers race), no buffering, flush every packet.
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error",
           "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32", "-analyzeduration", "0",
           "-f", "alaw", "-ar", "8000", "-ac", "1", "-i", "-", "-c:a", "copy",
           "-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0"]
    cmd += ["-rtsp_transport", "tcp", "-f", "rtsp", output] if output.startswith("rtsp") else ["-y", output]
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def on_audio(data):
        ff.stdin.write(data)
        ff.stdin.flush()

    c = client(on_audio=on_audio)
    try:
        c.connect()
        log("listening")
        while ff.poll() is None and not c.udt.closed:
            c.pump(0.02)                # hand each 40 ms audio frame to ffmpeg right away
    except (BrokenPipeError, GlnkError, OSError) as e:
        log("stopped:", e)
    except Stop:
        log("stop requested")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # finish the cleanup even if signalled again
        c.close()
        try:
            ff.stdin.close()
        except OSError:
            pass
        ff.wait(timeout=5)


def talk():
    """A-law on stdin -> doorbell speaker, until stdin closes."""
    c = client()
    fd = sys.stdin.fileno()
    buf = bytearray()
    talking = False
    try:
        c.connect()
        if not c.talk_start():
            log("talk request not acknowledged, sending anyway")
        talking = True
        log("talking")
        while not c.udt.closed:
            r, _, _ = select.select([fd], [], [], 0.02)
            if r:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break                       # go2rtc closed the backchannel
                buf += chunk
                while len(buf) >= FRAME:
                    c.talk_send(bytes(buf[:FRAME]))
                    del buf[:FRAME]
            c.pump(0)
    except (GlnkError, OSError) as e:
        log("stopped:", e)
    except Stop:
        log("stop requested")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if talking and not c.udt.closed:
                c.talk_stop()
                c.pump(0.1)
        except (GlnkError, OSError):
            pass
        c.close()
        log("session closed")


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "listen" and sys.argv[2] != "-":
        listen(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "listen":
        listen_pipe()
    elif len(sys.argv) >= 2 and sys.argv[1] == "talk":
        talk()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
