# Changelog

## 0.3.0

First public release.

- **Two-way audio in the Home Assistant dashboard** with go2rtc: `glnk_bridge/av.py` (`talk`
  backchannel and `listen`), standard library only, runs inside the go2rtc / Frigate container.
- `listen` streams WAV straight to go2rtc (no ffmpeg, no RTSP relay): starts in ~0.1 s instead of
  ~5 s and removes about two seconds of audio delay. `listen OUTPUT` (through ffmpeg) is still
  available.
- Sessions are closed cleanly on SIGTERM (talk stop, bye, UDT shutdown). With go2rtc
  `#killsignal=15`, the next call is no longer silenced by a stale talk session on the device.
- No default device IP anywhere: `GLNK_HOST` is required.
- Tools: `glnk_tool.py` (info, record, chime, unlock) replaces the first prototype;
  `latency.py` measures the doorbell's own audio round trip.
- Tests against a synthetic doorbell (UDT with packet loss, login, stream, talk, unlock, session
  close, FTP ring detection), run on GitHub Actions.
- Examples: go2rtc / Frigate configuration, dashboard card, notification automation.

## 0.1.0

- Ring detection through the doorbell's FTP snapshots, MQTT discovery, door unlock, chime,
  simulated ring.
