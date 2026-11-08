"""
Synthetic GLNK doorbell for tests: UDT v4 server + OWSP/TLV, written from the protocol notes
(docs/PROTOCOL.md). No captured traffic or vendor code is used.

    dev = FakeDoorbell(password="1234", unlock_code="9876", loss=0.05, echo=True).start()
    ... GlnkClient("127.0.0.1", dev.port, "admin", "1234") ...
    dev.stop()

Features: SYN-cookie handshake, ACK / NAK / retransmission, optional packet loss on the device
side, login check, live stream (synthetic H.264-like video frames with a counter + A-law audio),
talk (records what the client sends), lock, bye, shutdown. With `echo=True` the "microphone"
plays back what the "speaker" receives, so latency tools can be tested end to end.
"""
import math
import random
import socket
import struct
import threading
import time

SEQ_MAX = 0x7FFFFFFF
PAYLOAD = 1342
SILENCE = b"\xd5" * 320


def frame(tlvs):
    body = b"".join(struct.pack("<HH", t, len(v)) + v for t, v in tlvs)
    return struct.pack(">I", 4 + len(body)) + b"\0\0\0\0" + body


def video_payload(n, size):
    """Annex B looking frame carrying its index, so tests can check order and integrity."""
    head = b"\0\0\0\x01" + (b"\x65" if n % 10 == 0 else b"\x41") + struct.pack(">I", n)
    body = bytes((n + i) & 0xFF for i in range(size - len(head)))
    return head + body


class Session:
    def __init__(self, dev, addr, client_id, isn):
        self.dev, self.addr, self.client_id = dev, addr, client_id
        self.sid = random.randint(0x01000000, 0x7FFFFFFF)
        self.dev_seq = isn            # the device starts its data sequence at the client's ISN
        self.cli_next = isn
        self.cli_buf = bytearray()
        self.unacked = {}             # seq -> [packet, last send]
        self.msgno = 1
        self.logged_in = False
        self.talking = False
        self.closed = False
        self.video_n = 0
        self.last_media = 0.0
        self.last_video = 0.0
        self.last_ack = 0.0
        self.speaker = bytearray()    # talk audio waiting to be "heard" (echo mode)

    # -- sending
    def ctrl(self, typ, extra=0, body=b"\0\0\0\0"):
        self.dev.sock.sendto(struct.pack(">HHIII", 0x8000 | typ, 0, extra, 0, self.client_id) + body,
                             self.addr)

    def send_msg(self, data):
        chunks = [data[i:i + PAYLOAD] for i in range(0, len(data), PAYLOAD)]
        for i, c in enumerate(chunks):
            fl = (2 if i == 0 else 0) | (1 if i == len(chunks) - 1 else 0)
            pkt = struct.pack(">IIII", self.dev_seq, (fl << 30) | self.msgno, 0, self.client_id) + c
            self.unacked[self.dev_seq] = [pkt, time.monotonic()]
            if random.random() >= self.dev.loss:
                self.dev.sock.sendto(pkt, self.addr)
            else:
                self.dev.dropped += 1
            self.dev_seq = (self.dev_seq + 1) & SEQ_MAX
        self.msgno += 1

    # -- receiving
    def on_ctrl(self, typ, body):
        if typ == 2 and len(body) >= 4:                               # ACK
            ack = struct.unpack(">I", body[:4])[0] & SEQ_MAX
            for s in [s for s in self.unacked if ((ack - s) & SEQ_MAX) < (SEQ_MAX >> 1) and s != ack]:
                del self.unacked[s]
        elif typ == 3:                                                # NAK
            vals = struct.unpack(">%dI" % (len(body) // 4), body[:len(body) // 4 * 4])
            i = 0
            while i < len(vals):
                if vals[i] & 0x80000000 and i + 1 < len(vals):
                    a, b, i = vals[i] & SEQ_MAX, vals[i + 1], i + 2
                else:
                    a = b = vals[i]
                    i += 1
                s = a
                while ((b - s) & SEQ_MAX) < (SEQ_MAX >> 1):
                    if s in self.unacked:
                        self.dev.sock.sendto(self.unacked[s][0], self.addr)
                    s = (s + 1) & SEQ_MAX
        elif typ == 5:                                                # shutdown
            self.closed = True
            self.dev.events.append("shutdown")

    def on_data(self, pkt):
        if struct.unpack(">I", pkt[12:16])[0] != self.sid:
            return
        seq = struct.unpack(">I", pkt[:4])[0] & SEQ_MAX
        if seq != self.cli_next:
            return                                                    # client retransmits
        self.cli_next = (self.cli_next + 1) & SEQ_MAX
        self.cli_buf += pkt[16:]
        while len(self.cli_buf) >= 8:
            ln = struct.unpack(">I", self.cli_buf[:4])[0]
            if len(self.cli_buf) < 4 + ln:
                break
            body = bytes(self.cli_buf[8:4 + ln])
            del self.cli_buf[:4 + ln]
            tlvs, o = [], 0
            while o + 4 <= len(body):
                t, l = struct.unpack("<HH", body[o:o + 4])
                tlvs.append((t, body[o + 4:o + 4 + l]))
                o += 4 + l
            self.on_frame(tlvs)

    def on_frame(self, tlvs):
        d = dict(tlvs)
        dev = self.dev
        if 41 in d:
            v = d[41]
            user = v[1:33].rstrip(b"\0").decode()
            pw = v[33:58].split(b"\0")[0].decode()
            ok = (user == dev.user and pw == dev.password and v[60:64] == struct.pack("<I", 1)
                  and v[64] == 1 and v[65] == 2)
            dev.events.append("login ok" if ok else "login refused")
            if ok:
                self.logged_in = True
                info = b"LT0000000000000\0IPC01\0V0.0.0.0\0"
                self.send_msg(frame([(40, struct.pack("<I", 5)), (42, struct.pack("<i", 1)),
                                     (70, info), (203, b"\0" * 16)]))
            else:
                self.send_msg(frame([(40, struct.pack("<I", 5)), (42, struct.pack("<i", 7))]))
        elif 331 in d:
            val = struct.unpack("<I", d[331][4:8])[0]
            self.talking = val == 1
            dev.events.append("talk start" if val == 1 else "talk stop")
            if val == 1:
                self.send_msg(frame([(332, struct.pack("<I", 1) + b"\0" * 24)]))
        elif 98 in d and self.talking:
            dev.talk_audio += d[98]
            self.speaker += d[98]
        elif 425 in d:
            v = d[425]
            code = v[4:36].rstrip(b"\0").decode()
            ok = code == dev.unlock_code
            dev.events.append("unlock ok" if ok else "unlock refused")
            dev.unlocks.append(struct.unpack("<I", v[36:40])[0])
            self.send_msg(frame([(426, struct.pack("<hh", 1 if ok else -1, 0))]))
        elif 5005 in d:
            dev.events.append("bye")

    # -- periodic
    def tick(self, now):
        if now - self.last_ack > 0.02:
            self.ctrl(2, 1, struct.pack(">IIIIII", self.cli_next, 0, 0, 1000, 1, 1000))
            self.last_ack = now
        for item in self.unacked.values():
            if now - item[1] > 0.3:
                self.dev.sock.sendto(item[0], self.addr)
                item[1] = now
        if not (self.logged_in and self.dev.stream):
            return
        if now - self.last_media >= 0.04:                            # 40 ms of audio
            self.last_media = now
            if self.dev.echo and len(self.speaker) >= 320:
                audio = bytes(self.speaker[:320])
                del self.speaker[:320]
            else:
                audio = self.dev.background
            self.send_msg(frame([(97, b"\0" * 8), (98, audio)]))
        if now - self.last_video >= 0.09:                            # ~11 fps
            self.last_video = now
            self.send_msg(frame([(99, b"\0" * 12),
                                 (100 if self.video_n % 10 == 0 else 101,
                                  video_payload(self.video_n, self.dev.video_size))]))
            self.video_n += 1


class FakeDoorbell:
    def __init__(self, user="admin", password="1234", unlock_code="9876", loss=0.0, echo=False,
                 stream=True, video_size=3000):
        self.user, self.password, self.unlock_code = user, password, unlock_code
        self.loss, self.echo, self.stream, self.video_size = loss, echo, stream, video_size
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.005)
        self.port = self.sock.getsockname()[1]
        self.cookie = random.randint(1, 0xFFFFFFFF)
        self.sessions = {}
        self.events, self.unlocks = [], []
        self.talk_audio = bytearray()
        self.dropped = 0
        # quiet background noise for the "microphone"
        self.background = bytes(0xD5 ^ (1 if (i * 7919) % 13 == 0 else 0) for i in range(320))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(2)
        self.sock.close()

    def wait_for(self, event, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if event in self.events:
                return True
            time.sleep(0.01)
        return False

    def _handshake(self, pkt, addr):
        body = pkt[16:]
        if len(body) < 32:
            return
        isn = struct.unpack(">I", body[8:12])[0]
        req = struct.unpack(">i", body[20:24])[0]
        cid = struct.unpack(">I", body[24:28])[0]
        cookie = struct.unpack(">I", body[28:32])[0]
        hdr = struct.pack(">HHIII", 0x8000, 0, 0, 0, cid)
        if req == 1:
            self.sock.sendto(hdr + body[:28] + struct.pack(">I", self.cookie) + body[32:], addr)
        elif req == -1 and cookie == self.cookie:
            s = self.sessions.get(addr)
            if s is None:
                s = self.sessions[addr] = Session(self, addr, cid, isn)
            self.sock.sendto(hdr + body[:20] + struct.pack(">iII", -1, s.sid, self.cookie) + body[32:], addr)

    def _run(self):
        while not self._stop.is_set():
            try:
                pkt, addr = self.sock.recvfrom(65535)
                if len(pkt) >= 16:
                    if pkt[0] & 0x80:
                        typ = struct.unpack(">H", pkt[:2])[0] & 0x7FFF
                        if typ == 0:
                            self._handshake(pkt, addr)
                        elif addr in self.sessions:
                            self.sessions[addr].on_ctrl(typ, pkt[16:])
                    elif addr in self.sessions:
                        self.sessions[addr].on_data(pkt)
            except (socket.timeout, OSError):
                pass
            now = time.monotonic()
            for addr, s in list(self.sessions.items()):
                if s.closed:
                    del self.sessions[addr]
                else:
                    s.tick(now)


def tone(freq=1000, seconds=0.2, amp=6000, rate=8000):
    """A-law encoded sine, multiple of 320 bytes (used by tests)."""
    from glnk_bridge.glnk import alaw_encode_sample
    n = int(seconds * rate) // 320 * 320
    return bytes(alaw_encode_sample(int(amp * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
