# GLNK local protocol notes

Observed on a GLNK/Goolink doorbell (`IPC01`, firmware 2.3.20.15) used with the ControlCam2 app,
by capturing the app's LAN traffic and reading the app's public Java wrapper. No vendor code,
binary or document is included in this repository.

## Transport

- **UDT v4** over UDP (standard open protocol). The device listened on UDP **59613** here.
- Client/server handshake in two steps with a SYN cookie: version 4, socket type 1, MSS 1386,
  flow window 1000. The device starts its own data sequence at the client's initial sequence number.
- Control packets used: handshake (0), keep-alive (1), ACK (2), NAK (3), shutdown (5), ACK2 (6).
- Data packets carry message boundary flags; frames below may span several packets.

## Frames ("OWSP")

```
uint32 BE  length   (bytes that follow)
uint32     0
TLV...     type uint16 LE | length uint16 LE | value
```

## Messages

| Direction | TLV | Meaning |
|---|---|---|
| → | 40 (4 bytes, value 5) + 41 (68 bytes) | login. 41 = `[0]` 0, `[1:33]` user, `[33:58]` password, `[60:64]` int32 1, `[64]` 1, `[65]` 2. Without the trailing fields the device answers code 7 |
| ← | 40 + 42 (int32, 1 = OK) + 70 (device id, model, version strings) + 203 (stream format) | login reply. Streaming starts right after |
| ← | 99 (12-byte header) + 100 / 101 | H.264 Annex B key frame / other frame (320x240 on this device) |
| ← | 97 (8 bytes) + 98 (320 bytes) | visitor audio, G.711 A-law 8 kHz, 40 ms |
| → | 331 (4 zero bytes + int32: 1 start, 2 stop) | talk request; reply 332 (starts with int32 1, then audio parameters such as 8000 Hz) |
| → | 97 (`01 00 00 00 00 00 00 00`) + 98 (320 bytes A-law) | voice to the doorbell speaker, every 40 ms |
| → | 425 (44 bytes LE: device id 0, unlock code char[32], delay int32, channel u8 0, action u8 1, 2 reserved) | open the door; reply 426 int16 (1 = success) |
| → | 5005 (empty) | bye |

## Closing a session

Close in this order: talk stop (331 = 2) if talking, bye (5005), then UDT shutdown. If a client
disappears without it (killed process), the device keeps the old session for a while and **ignores
the voice of the next talk session**, even though that session logs in and starts normally. The same device
handles a separate listening session and talking session at the same time without trouble.

## Timing

- Login answer: well under a second on the LAN.
- Audio round trip measured on the device itself (A-law tone sent with 331/97/98, detected in the
  received 98 frames): about **0.22 s**. The doorbell does not add noticeable buffering; latency seen
  in a browser comes from the media chain (transcoding, RTSP relays, WebRTC jitter buffers).

## Doorbell press

The device sends nothing on the GLNK session, nothing on the LAN and no extra message to the cloud
when the button is pressed (tested). Its web UI (`http://<device>/`, user `admin`) has
*Alarm Settings → Sensor Detection* with snapshot linkage to **FTP**: on each press the device
uploads a few JPEG pictures (`YYYY-MM-DD/<name>/1/HH_MM_SS.jpg`), one FTP session per picture.
That is what the bridge listens to.

The outdoor ring-back beep is muted while the ControlCam2 app is connected to the device. A third-party
client connected for a recording did not mute it in tests.
