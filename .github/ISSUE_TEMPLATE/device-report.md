---
name: Device report
about: Tell whether glnk-bridge works with your doorbell or camera
labels: device
---

**Device**
- Brand / model as sold:
- App used with it (ControlCam2, another one?):
- Model and firmware from the web UI or `tools/glnk_tool.py --host <ip> info`
  (remove the device id / serial):

**What works**
- [ ] login (`glnk_tool.py info`)
- [ ] video / audio (`glnk_tool.py record`)
- [ ] chime on the speaker (`glnk_tool.py chime`)
- [ ] door unlock
- [ ] FTP snapshots on doorbell press
- [ ] two-way audio in the dashboard

**Logs**
Paste relevant lines. Never paste passwords, unlock codes, public IPs or device ids.
