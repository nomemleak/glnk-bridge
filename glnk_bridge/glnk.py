"""
Minimal client for GLNK (Goolink / "ControlCam2") doorbells and cameras, LAN only.

Transport   : UDT v4 over UDP (small client implementation below).
Application : OWSP frames = [length u32 BE][4 zero bytes][TLV...],
              TLV = type u16 LE, length u16 LE, value.

Only the Python standard library is used.
"""
import math
import random
import socket
import struct
import threading
import time

# --------------------------------------------------------------------------- UDT

SEQ_MAX = 0x7FFFFFFF


def seq_add(s, n=1):
    return (s + n) & SEQ_MAX


def seq_diff(a, b):
    """a - b with wrap-around."""
    d = (a - b) & SEQ_MAX
    return d - (SEQ_MAX + 1) if d > (SEQ_MAX >> 1) else d


class GlnkError(Exception):
    pass


class UDT:
    """Minimal UDT v4 client: handshake, reliable in-order receive, send with retransmission."""

    MSS = 1386
    PAYLOAD = MSS - 28 - 16          # data bytes per packet
    FLOW_WINDOW = 1000
    CTL_HANDSHAKE, CTL_KEEPALIVE, CTL_ACK, CTL_NAK, CTL_SHUTDOWN, CTL_ACK2 = 0, 1, 2, 3, 5, 6

    def __init__(self, host, port):
        self.addr = (socket.gethostbyname(host), port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.t0 = time.monotonic()
        self.my_id = random.randint(0x01000000, 0x7FFFFFFF)
        self.isn = random.randint(0, SEQ_MAX)
        self.peer_id = 0
        self.snd_next = self.isn
        self.msgno = 1
        self.unacked = {}            # seq -> [packet, last send time]
        self.last_sent = 0.0
        self.rcv_next = None
        self.rcv_pending = {}
        self.stream = bytearray()
        self.ack_no = 0
        self.last_ack_val = None
        self.last_ack_time = 0.0
        self.last_nak_time = 0.0
        self.closed = False

    def _ts(self):
        return int((time.monotonic() - self.t0) * 1e6) & 0xFFFFFFFF

    def _send_raw(self, data):
        self.sock.sendto(data, self.addr)
        self.last_sent = time.monotonic()

    def _ctrl(self, typ, extra=0, body=b"\0\0\0\0", dst=None):
        hdr = struct.pack(">HHIII", 0x8000 | typ, 0, extra, self._ts(),
                          self.peer_id if dst is None else dst)
        self._send_raw(hdr + body)

    def _handshake_body(self, req_type, cookie):
        peer_ip = socket.inet_aton(self.addr[0])[::-1] + b"\0" * 12
        return struct.pack(">IIIIIiII", 4, 1, self.isn, self.MSS, self.FLOW_WINDOW,
                           req_type, self.my_id, cookie) + peer_ip

    def _recv_for(self, seconds):
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            self.sock.settimeout(left)
            try:
                data, src = self.sock.recvfrom(65535)
            except socket.timeout:
                return
            if src == self.addr:
                yield data

    @staticmethod
    def _is_handshake(pkt):
        return len(pkt) >= 64 and pkt[0] & 0x80 and struct.unpack(">H", pkt[:2])[0] & 0x7FFF == 0

    def connect(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        cookie = None
        while cookie is None:                       # 1. request -> SYN cookie
            if time.monotonic() > deadline:
                raise GlnkError("no answer to the UDT handshake (wrong IP/port or device offline)")
            self._ctrl(self.CTL_HANDSHAKE, body=self._handshake_body(1, 0), dst=0)
            for pkt in self._recv_for(0.5):
                if self._is_handshake(pkt):
                    cookie = struct.unpack(">I", pkt[44:48])[0]
                    break
        while True:                                 # 2. confirm -> peer socket id
            if time.monotonic() > deadline:
                raise GlnkError("UDT handshake not completed")
            self._ctrl(self.CTL_HANDSHAKE, body=self._handshake_body(-1, cookie), dst=0)
            for pkt in self._recv_for(0.5):
                if self._is_handshake(pkt):
                    req_type = struct.unpack(">i", pkt[36:40])[0]
                    if req_type == -1:
                        self.peer_id = struct.unpack(">I", pkt[40:44])[0]
                        self.rcv_next = struct.unpack(">I", pkt[24:28])[0]
                        return
                    if req_type == 1:
                        cookie = struct.unpack(">I", pkt[44:48])[0]

    def send_message(self, data):
        if self.closed:
            raise GlnkError("connection closed by the device")
        chunks = [data[i:i + self.PAYLOAD] for i in range(0, len(data), self.PAYLOAD)] or [b""]
        for i, chunk in enumerate(chunks):
            flags = (0x2 if i == 0 else 0) | (0x1 if i == len(chunks) - 1 else 0)
            hdr = struct.pack(">IIII", self.snd_next, (flags << 30) | (self.msgno & 0x1FFFFFFF),
                              self._ts(), self.peer_id)
            pkt = hdr + chunk
            self.unacked[self.snd_next] = [pkt, time.monotonic()]
            self._send_raw(pkt)
            self.snd_next = seq_add(self.snd_next)
        self.msgno = ((self.msgno + 1) & 0x1FFFFFFF) or 1

    def poll(self, seconds=0.0):
        end = time.monotonic() + seconds
        while True:
            left = max(0.0, end - time.monotonic())
            self.sock.settimeout(left if left > 0 else 0.0005)
            try:
                data, src = self.sock.recvfrom(65535)
                if src == self.addr:
                    self._handle(data)
            except (socket.timeout, BlockingIOError):
                pass
            self._timers()
            if time.monotonic() >= end:
                return

    def _handle(self, pkt):
        if len(pkt) < 16:
            return
        if pkt[0] & 0x80:
            typ = struct.unpack(">H", pkt[0:2])[0] & 0x7FFF
            extra = struct.unpack(">I", pkt[4:8])[0]
            body = pkt[16:]
            if typ == self.CTL_ACK and len(body) >= 4:
                ack = struct.unpack(">I", body[:4])[0] & SEQ_MAX
                for s in [s for s in self.unacked if seq_diff(s, ack) < 0]:
                    del self.unacked[s]
                if len(body) > 4:
                    self._ctrl(self.CTL_ACK2, extra=extra)
            elif typ == self.CTL_NAK:
                vals = struct.unpack(">%dI" % (len(body) // 4), body[:len(body) // 4 * 4])
                i = 0
                while i < len(vals):
                    v = vals[i]
                    if v & 0x80000000 and i + 1 < len(vals):
                        start, stop, i = v & SEQ_MAX, vals[i + 1], i + 2
                    else:
                        start = stop = v
                        i += 1
                    s = start
                    while seq_diff(s, stop) <= 0:
                        if s in self.unacked:
                            self._send_raw(self.unacked[s][0])
                            self.unacked[s][1] = time.monotonic()
                        s = seq_add(s)
            elif typ == self.CTL_SHUTDOWN:
                self.closed = True
            return
        seq = struct.unpack(">I", pkt[0:4])[0] & SEQ_MAX
        if self.rcv_next is None:
            self.rcv_next = seq
        if seq_diff(seq, self.rcv_next) < 0 or seq in self.rcv_pending:
            return
        self.rcv_pending[seq] = pkt[16:]
        while self.rcv_next in self.rcv_pending:
            self.stream += self.rcv_pending.pop(self.rcv_next)
            self.rcv_next = seq_add(self.rcv_next)
        if self.rcv_pending and time.monotonic() - self.last_nak_time > 0.02:
            first = self.rcv_next
            last = seq_add(min(self.rcv_pending, key=lambda s: seq_diff(s, self.rcv_next)), -1)
            body = struct.pack(">I", first) if first == last else struct.pack(">II", first | 0x80000000, last)
            self._ctrl(self.CTL_NAK, body=body)
            self.last_nak_time = time.monotonic()

    def _timers(self):
        now = time.monotonic()
        if self.rcv_next is not None and (
                (self.rcv_next != self.last_ack_val and now - self.last_ack_time > 0.01)
                or now - self.last_ack_time > 0.1):
            self.ack_no = ((self.ack_no + 1) & 0x7FFFFFFF) or 1
            avail = max(2, self.FLOW_WINDOW - len(self.rcv_pending) - 1)
            self._ctrl(self.CTL_ACK, extra=self.ack_no,
                       body=struct.pack(">IIIIII", self.rcv_next, 10000, 5000, avail, 1, 1000))
            self.last_ack_val = self.rcv_next
            self.last_ack_time = now
        for item in self.unacked.values():
            if now - item[1] > 0.3:
                self._send_raw(item[0])
                item[1] = now
        if now - self.last_sent > 1.0:
            self._ctrl(self.CTL_KEEPALIVE)

    def close(self):
        if not self.closed and self.peer_id:
            for _ in range(3):
                self._ctrl(self.CTL_SHUTDOWN)
        self.closed = True
        self.sock.close()


# --------------------------------------------------------------------------- OWSP / TLV

T_VERSION = 40
T_LOGIN_REQ = 41
T_LOGIN_RSP = 42
T_DEVICE_INFO = 70
T_STREAM_FORMAT = 203
T_AUDIO_INFO = 97
T_AUDIO_DATA = 98        # G.711 A-law, 8 kHz, 320 bytes = 40 ms
T_VIDEO_INFO = 99
T_VIDEO_IFRAME = 100     # H.264 Annex B
T_VIDEO_PFRAME = 101
T_TALK_REQ = 331         # value: 4 zero bytes + int32 (1 = start, 2 = stop)
T_TALK_RSP = 332
T_LOCK_REQ = 425
T_LOCK_RSP = 426
T_BYE = 5005


def owsp_frame(tlvs):
    body = b"".join(struct.pack("<HH", t, len(v)) + v for t, v in tlvs)
    return struct.pack(">I", 4 + len(body)) + b"\0\0\0\0" + body


def owsp_pop(stream):
    """Pop complete frames from a bytearray; returns a list of TLV lists."""
    frames = []
    while len(stream) >= 8:
        ln = struct.unpack(">I", stream[:4])[0]
        if ln < 4 or ln > 4_000_000:
            raise GlnkError("desynchronised OWSP stream (length %d)" % ln)
        if len(stream) < 4 + ln:
            break
        body = bytes(stream[8:4 + ln])
        del stream[:4 + ln]
        tlvs, o = [], 0
        while o + 4 <= len(body):
            t, l = struct.unpack("<HH", body[o:o + 4])
            tlvs.append((t, body[o + 4:o + 4 + l]))
            o += 4 + l
        frames.append(tlvs)
    return frames


def login_payload(user, password):
    """T41 value as sent by ControlCam2 (68 bytes). Trailing fields are required (else code 7)."""
    v = bytearray(68)
    v[1:33] = user.encode().ljust(32, b"\0")[:32]
    v[33:58] = password.encode().ljust(25, b"\0")[:25]
    v[60:64] = struct.pack("<I", 1)
    v[64] = 1
    v[65] = 2
    return bytes(v)


# --------------------------------------------------------------------------- G.711 A-law

_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def alaw_encode_sample(pcm):
    pcm >>= 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    for seg, end in enumerate(_SEG_AEND):
        if pcm <= end:
            break
    else:
        return 0x7F ^ mask
    return ((seg << 4) | (((pcm >> 1) if seg < 2 else (pcm >> seg)) & 0x0F)) ^ mask


def chime(rate=8000):
    """Short two-tone chime as A-law bytes (multiple of 320)."""
    out = bytearray()
    for freq, dur in ((880, 0.45), (0, 0.05), (660, 0.6)):
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / 200, (n - i) / 400) if freq else 0.0
            out.append(alaw_encode_sample(int(12000 * env * math.sin(2 * math.pi * freq * i / rate))))
    out += b"\xd5" * (-len(out) % 320)
    return bytes(out)


# --------------------------------------------------------------------------- client

class GlnkClient:
    """One session with the device. Not thread-safe: use one client per thread."""

    def __init__(self, host, port, user, password, on_video=None, on_audio=None):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.on_video, self.on_audio = on_video, on_audio
        self.udt = None
        self.replies = {}
        self.device_info = []

    # -- plumbing
    def _send(self, tlvs):
        self.udt.send_message(owsp_frame(tlvs))

    def pump(self, seconds):
        self.udt.poll(seconds)
        for tlvs in owsp_pop(self.udt.stream):
            for t, v in tlvs:
                if t in (T_VIDEO_IFRAME, T_VIDEO_PFRAME):
                    if self.on_video:
                        self.on_video(v, t == T_VIDEO_IFRAME)
                elif t == T_AUDIO_DATA:
                    if self.on_audio:
                        self.on_audio(v)
                elif t not in (T_AUDIO_INFO, T_VIDEO_INFO):
                    self.replies[t] = v

    def _wait(self, t, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.udt.closed:
            if t in self.replies:
                return self.replies.pop(t)
            self.pump(0.05)
        return None

    # -- API
    def connect(self):
        self.udt = UDT(self.host, self.port)
        self.udt.connect()
        self._send([(T_VERSION, struct.pack("<I", 5)), (T_LOGIN_REQ, login_payload(self.user, self.password))])
        rsp = self._wait(T_LOGIN_RSP)
        if rsp is None:
            raise GlnkError("no answer to login")
        code = struct.unpack("<i", rsp[:4])[0]
        if code != 1:
            raise GlnkError("login refused (code %d)" % code)
        info = self.replies.pop(T_DEVICE_INFO, b"")
        self.device_info = [s.decode("ascii", "replace") for s in info.split(b"\0") if len(s) >= 3]
        return self.device_info

    def unlock(self, code, delay=1, timeout=5.0):
        payload = (struct.pack("<I", 0) + code.encode().ljust(32, b"\0")[:32]
                   + struct.pack("<I", delay) + bytes([0, 1]) + b"\0\0")
        self._send([(T_LOCK_REQ, payload)])
        rsp = self._wait(T_LOCK_RSP, timeout)
        if rsp is None:
            raise GlnkError("no answer to the unlock command")
        return struct.unpack("<h", rsp[:2])[0] == 1

    def talk_start(self):
        self._send([(T_TALK_REQ, struct.pack("<II", 0, 2))])
        self.pump(0.07)
        self._send([(T_TALK_REQ, struct.pack("<II", 0, 1))])
        return self._wait(T_TALK_RSP) is not None

    def talk_send(self, alaw320):
        self._send([(T_AUDIO_INFO, b"\x01\0\0\0\0\0\0\0"), (T_AUDIO_DATA, alaw320)])

    def talk_stop(self):
        self._send([(T_TALK_REQ, struct.pack("<II", 0, 2))])

    def close(self):
        if not self.udt:
            return
        try:
            if not self.udt.closed:
                self._send([(T_BYE, b"")])
                self.pump(0.2)
        except (OSError, GlnkError):
            pass
        finally:
            self.udt.close()


class GlnkLock:
    """Serialises access to the device across threads (one session at a time)."""
    lock = threading.Lock()
