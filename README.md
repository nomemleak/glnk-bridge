# glnk-bridge

Local Home Assistant bridge for **GLNK / Goolink doorbells** (the ones sold under many brands and
controlled with the *ControlCam2* app). No cloud, no vendor app, no open port.

- 🔔 **Doorbell press** → Home Assistant event + picture of the visitor (phone notification with photo)
- 🚪 **Open the door** from Home Assistant or straight from the notification
- 📞 **Talk to the visitor** from the Home Assistant dashboard: live video, visitor audio and your
  voice on the doorbell speaker (two-way audio through go2rtc / WebRTC)
- 🧪 **Simulate a ring** and **play a chime** to test everything without going outside

Entities appear automatically in Home Assistant through **MQTT discovery**. The GLNK protocol is
reimplemented from scratch in pure Python, see [docs/PROTOCOL.md](docs/PROTOCOL.md).

> **Status:** used daily on one device (`IPC01`, firmware 2.3.20.15). Other GLNK devices probably
> speak the same protocol, but this is untested: please open an issue with your model, whether it
> works or not.

## How it works

```
                    FTP: pictures on each press
   doorbell ─────────────────────────────────────► glnk-bridge ──MQTT──► Home Assistant ──► phones
       ▲  ▲          GLNK (UDT/UDP, LAN): unlock     │    ▲                    │ (notification,
       │  └──────────────────────────────────────────┘    └── buttons ────────┘  Open door)
       │
       │  GLNK: visitor audio + your voice      go2rtc (Frigate or standalone)
       └─────────────────────────────────────── av.py ──WebRTC──► dashboard card (video + call)
```

- The doorbell never reports a press on the network, but its web UI can **upload snapshots by FTP**
  when its doorbell input triggers. glnk-bridge embeds a small FTP server that only accepts the
  doorbell's IP and turns each burst of pictures into one `ring` event.
- Door unlock and the chime use the device's local **GLNK protocol** (UDT over UDP).
- Two-way audio is done by `glnk_bridge/av.py`, a dependency-free helper started by
  [go2rtc](https://github.com/AlexxIT/go2rtc) `exec` sources: one process streams the visitor's
  audio, another sends your voice to the speaker. GLNK sessions only exist while someone is watching
  or talking.

## Entities

With `DEVICE_NAME=Doorbell`:

| Entity | Type | |
|---|---|---|
| `event.doorbell_ring` | event (`doorbell`) | fires on each press |
| `binary_sensor.doorbell_ringing` | binary sensor | on for `RING_HOLD` seconds |
| `sensor.doorbell_last_ring` | timestamp | |
| `image.doorbell_visitor` | image | latest snapshot |
| `image.doorbell_visitor_1` … `_4` | image | last visitors, newest first (one picture per ring) |
| `sensor.doorbell_visitor_1_time` … `_4_time` | timestamp | time of each of those rings |
| `button.doorbell_open_door` | button | unlocks the door |
| `sensor.doorbell_last_unlock` | sensor | result of the last unlock |
| `button.doorbell_play_chime` | button | plays a short chime on the doorbell |
| `button.doorbell_simulate_ring` | button (diagnostic) | fake ring with the last picture |

## Setup

Requirements: Docker, an MQTT broker (e.g. Mosquitto) connected to Home Assistant, and for live
audio go2rtc (standalone, or the one inside [Frigate](https://frigate.video)).

### 1. Doorbell web UI (`http://<doorbell-ip>/`, user `admin`)

Give the doorbell a fixed IP (DHCP reservation), then:

- *Network Settings → FTP*, main server: IP of the machine running glnk-bridge, port `2121`,
  catalog `/`, user / password = `FTP_USER` / `FTP_PASSWORD` from your `.env`.
- *Alarm Settings → Sensor Detection*: **Enable**, time 0:00–23:59, **Snapshot** checked, linked
  to **Ftp**.

### 2. MQTT account for the bridge

With Mosquitto in Docker, for example:

```bash
docker compose exec mosquitto mosquitto_passwd /mosquitto/config/passwd glnk
docker compose restart mosquitto
```

### 3. Run glnk-bridge

```bash
git clone https://github.com/nomemleak/glnk-bridge && cd glnk-bridge
cp .env.example .env && chmod 600 .env && vim .env     # IPs, passwords, unlock code
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Expected: `FTP server listening on 0.0.0.0:2121` then `MQTT connected`. Press the doorbell:
`RING (...)`. The *Doorbell* device appears under *Settings → Devices → MQTT*.

### 4. Live video and two-way audio (go2rtc)

See [examples/go2rtc.yaml](examples/go2rtc.yaml) and, with Frigate,
[examples/frigate-compose.yaml](examples/frigate-compose.yaml). In short:

- mount `glnk_bridge/` into the go2rtc / Frigate container and pass `GLNK_HOST` / `GLNK_PASSWORD`;
- enable `exec` sources with `GO2RTC_ALLOW_ARBITRARY_EXEC=true` (read the warning below);
- add a stream with the backchannel **first**, the video, then the visitor audio:

```yaml
doorbell_live:
  - "exec:python3 /glnk/av.py talk#backchannel=1#audio=alaw/8000#killsignal=15#killtimeout=3"
  - rtsp://127.0.0.1:8554/doorbell
  - "exec:python3 /glnk/av.py listen#killsignal=15#killtimeout=3"
```

- publish the WebRTC port (8555 TCP/UDP) on the LAN, and to your VPN clients if you use one.

### 5. Home Assistant dashboard and notification

- Dashboard card with live view, call button and "Open the door":
  [examples/doorbell_card.yaml](examples/doorbell_card.yaml) (needs
  [Advanced Camera Card](https://github.com/dermotduffy/advanced-camera-card)).
- Pictures of the last visitors with the time of each ring:
  [examples/visitor_history_card.yaml](examples/visitor_history_card.yaml).
- Notification with the visitor's picture and an "Open the door" action protected by Face ID /
  passcode: [examples/doorbell_notification.yaml](examples/doorbell_notification.yaml).

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `GLNK_HOST` | *(required)* | doorbell IP |
| `GLNK_PORT` | `59613` | GLNK UDP port |
| `GLNK_USER` / `GLNK_PASSWORD` | `admin` / *(required)* | device account (on this model the password is the 4-digit code) |
| `GLNK_UNLOCK_CODE` | *(empty)* | door unlock code; the button reports an error without it |
| `GLNK_UNLOCK_DELAY` | `1` | unlock delay sent to the device |
| `FTP_PORT` / `FTP_BIND` | `2121` / `0.0.0.0` | embedded FTP server |
| `FTP_USER` / `FTP_PASSWORD` | `doorbell` / *(required)* | account typed in the doorbell web UI |
| `FTP_ALLOWED_IP` | `GLNK_HOST` | only this IP may connect |
| `FTP_PASSIVE_PORTS` | `60000-60010` | |
| `DATA_DIR` / `KEEP_DAYS` | `/data` / `7` | received pictures and how long to keep them |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | |
| `MQTT_USER` / `MQTT_PASSWORD` | *(empty)* | |
| `MQTT_PREFIX` / `HA_DISCOVERY_PREFIX` | `glnk` / `homeassistant` | |
| `DEVICE_ID` / `DEVICE_NAME` | `doorbell` / `Doorbell` | entity ids are derived from the name |
| `RING_GAP` | `20` | uploads closer than this (seconds) count as the same ring |
| `RING_HOLD` | `15` | seconds the `ringing` sensor stays on |
| `HISTORY_SIZE` | `4` | number of last visitors kept (in `DATA_DIR/history`, kept across restarts, not affected by `KEEP_DAYS`); `0` disables it |
| `LOG_LEVEL` | `INFO` | |
| `TZ` | | e.g. `Europe/Paris`, for log timestamps |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No call button in the card on first load | the card asks go2rtc whether the stream accepts audio before the `exec` source has started: add `capabilities: force: [2-way-audio]` to the camera (see the example card) |
| Your voice only works on the first call | `exec` processes killed with SIGKILL leave a stale talk session on the doorbell: add `#killsignal=15#killtimeout=3` to both `exec` lines |
| `codecs not matched` in go2rtc logs | the backchannel defaults to 16 kHz PCM: keep `#audio=alaw/8000` |
| "Two-way audio unavailable" | the backchannel line must be the **first** source of the stream |
| `exec` sources refused | `GO2RTC_ALLOW_ARBITRARY_EXEC=true` missing |
| Frigate: `Invalid substitution` | write `{{output}}` instead of `{output}` (only needed for `listen {output}`) |
| Voice works on Wi-Fi but not remotely | the WebRTC port (8555 TCP/UDP) is not reachable through your VPN |
| The doorbell's own outdoor ring-back beep is gone | it is muted while the ControlCam2 app is connected; close the app |
| Delay | the doorbell itself loops speaker → microphone in ~0.2 s (`tools/latency.py`); the rest is WebRTC and the phone |

## Tools

- `tools/glnk_tool.py`: command-line client (`info`, `record`, `chime`, `unlock`).
- `tools/latency.py`: measures the doorbell's own audio round trip (beep on the speaker, detected by
  its microphone).
- `tools/onvif_events.py`: dumps ONVIF event topics and events of a device.

## Development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

The tests run against `tests/fake_device.py`, a synthetic doorbell written from the protocol notes
(UDT with simulated packet loss, login, stream, talk, unlock, session close). No real device or
captured traffic is needed.

## Security notes

- The GLNK protocol is **unencrypted**: the device password and unlock code travel in clear on the
  LAN. Keep the doorbell on a trusted or isolated network, never expose it to the Internet, and
  remove any port forwarding or UPnP rule the vendor app may have created.
- The FTP password also travels in clear (LAN only). The FTP account can only write, and only the
  doorbell IP is accepted.
- `GO2RTC_ALLOW_ARBITRARY_EXEC=true` lets anyone who can edit the go2rtc configuration run commands
  in that container. Never expose the go2rtc API / web UI (port 1984) or Frigate's config editor.
- Protect the "Open door" button: non-admin Home Assistant accounts for the family, remote access
  only through a VPN, confirmation in dashboards and authentication in notifications.

## Legal

Independent project, not affiliated with Goolink, Langtao or any brand. Built for interoperability
with hardware I own. No vendor software, firmware, document or captured traffic is redistributed
here.

## License

[MIT](LICENSE)

---

*AI disclosure: the code, tests and documentation were written with an AI assistant, under my
direction. I did the protocol captures, the design decisions and all the testing on real hardware.*
