"""
glnk-bridge: turns a GLNK (Goolink / ControlCam2) doorbell into Home Assistant entities.

- Doorbell press  : the device uploads snapshots by FTP on each press (Sensor Detection
                    -> Snapshot -> Ftp). A built-in FTP server, restricted to the device IP,
                    receives them and fires an MQTT event + publishes the picture.
- Door unlock     : MQTT button -> GLNK command 425 on the LAN.
- Chime / test    : MQTT buttons to play a chime on the doorbell speaker and to simulate a ring.

Everything is exposed with MQTT discovery, so entities appear in Home Assistant automatically.
"""
import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from .glnk import GlnkClient, GlnkError, chime

log = logging.getLogger("glnk-bridge")


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit("missing required environment variable %s" % name)
    return v


class Config:
    def __init__(self):
        self.glnk_host = env("GLNK_HOST", required=True)
        self.glnk_port = int(env("GLNK_PORT", "59613"))
        self.glnk_user = env("GLNK_USER", "admin")
        self.glnk_password = env("GLNK_PASSWORD", required=True)
        self.unlock_code = env("GLNK_UNLOCK_CODE", "")
        self.unlock_delay = int(env("GLNK_UNLOCK_DELAY", "1"))
        self.ftp_bind = env("FTP_BIND", "0.0.0.0")
        self.ftp_port = int(env("FTP_PORT", "2121"))
        self.ftp_user = env("FTP_USER", "doorbell")
        self.ftp_password = env("FTP_PASSWORD", required=True)
        self.ftp_allowed_ip = env("FTP_ALLOWED_IP", self.glnk_host)
        lo, hi = env("FTP_PASSIVE_PORTS", "60000-60010").split("-")
        self.ftp_passive = range(int(lo), int(hi) + 1)
        self.data_dir = env("DATA_DIR", "/data")
        self.keep_days = float(env("KEEP_DAYS", "7"))
        self.mqtt_host = env("MQTT_HOST", "127.0.0.1")
        self.mqtt_port = int(env("MQTT_PORT", "1883"))
        self.mqtt_user = env("MQTT_USER", "")
        self.mqtt_password = env("MQTT_PASSWORD", "")
        self.topic = env("MQTT_PREFIX", "glnk").rstrip("/")
        self.discovery = env("HA_DISCOVERY_PREFIX", "homeassistant").rstrip("/")
        self.device_id = env("DEVICE_ID", "doorbell")
        self.device_name = env("DEVICE_NAME", "Doorbell")
        self.ring_gap = float(env("RING_GAP", "20"))       # uploads closer than this = same ring
        self.ring_hold = float(env("RING_HOLD", "15"))     # binary sensor stays on this long
        self.history_size = max(0, int(env("HISTORY_SIZE", "4")))   # pictures of the last visitors


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = "%s/%s" % (cfg.topic, cfg.device_id)
        self.last_ring = 0.0
        self.ring_off_timer = None
        self.device_lock = threading.Lock()          # one GLNK session at a time
        self.ftp_dir = os.path.join(cfg.data_dir, "ftp")
        self.last_jpg = os.path.join(cfg.data_dir, "last.jpg")
        self.hist_dir = os.path.join(cfg.data_dir, "history")
        self.hist_file = os.path.join(self.hist_dir, "history.json")
        self.hist_lock = threading.Lock()
        os.makedirs(self.ftp_dir, exist_ok=True)
        os.makedirs(self.hist_dir, exist_ok=True)
        self.history = self._load_history()          # newest first: [{"time": iso, "file": name}]
        if not self.history and not os.path.exists(self.hist_file):
            self._import_history()

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="glnk-bridge-" + cfg.device_id)
        if cfg.mqtt_user:
            self.mqtt.username_pw_set(cfg.mqtt_user, cfg.mqtt_password)
        self.mqtt.will_set(self.base + "/status", "offline", retain=True)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    # ------------------------------------------------------------------ MQTT
    def _device(self):
        return {"identifiers": ["glnk_" + self.cfg.device_id], "name": self.cfg.device_name,
                "manufacturer": "Goolink (GLNK)", "model": "GLNK doorbell"}

    def _discover(self):
        d, b = self.cfg.discovery, self.base
        common = {"device": self._device(), "availability_topic": b + "/status"}
        uid = "glnk_%s_" % self.cfg.device_id
        entities = [
            ("event", "ring", {"name": "Ring", "state_topic": b + "/event", "event_types": ["ring"],
                               "device_class": "doorbell"}),
            ("binary_sensor", "ringing", {"name": "Ringing", "state_topic": b + "/ringing",
                                          "icon": "mdi:doorbell"}),
            ("sensor", "last_ring", {"name": "Last ring", "state_topic": b + "/last_ring",
                                     "device_class": "timestamp"}),
            ("image", "visitor", {"name": "Visitor", "image_topic": b + "/snapshot",
                                  "content_type": "image/jpeg"}),
            ("sensor", "last_unlock", {"name": "Last unlock", "state_topic": b + "/unlock/state",
                                       "icon": "mdi:door-open"}),
            ("button", "unlock", {"name": "Open door", "command_topic": b + "/unlock/set",
                                  "icon": "mdi:door-open"}),
            ("button", "chime", {"name": "Play chime", "command_topic": b + "/chime/set",
                                 "icon": "mdi:bell-ring"}),
            ("button", "test_ring", {"name": "Simulate ring", "command_topic": b + "/test_ring/set",
                                     "icon": "mdi:doorbell-video", "entity_category": "diagnostic"}),
        ]
        for i in range(1, self.cfg.history_size + 1):
            entities += [
                ("image", "visitor_%d" % i, {"name": "Visitor %d" % i, "image_topic": "%s/history/%d/image" % (b, i),
                                             "content_type": "image/jpeg", "icon": "mdi:account-clock"}),
                ("sensor", "visitor_%d_time" % i, {"name": "Visitor %d time" % i,
                                                   "state_topic": "%s/history/%d/time" % (b, i),
                                                   "device_class": "timestamp", "icon": "mdi:clock-outline"}),
            ]
        for platform, key, conf in entities:
            payload = dict(common, unique_id=uid + key, **conf)
            self.mqtt.publish("%s/%s/glnk_%s/%s/config" % (d, platform, self.cfg.device_id, key),
                              json.dumps(payload), retain=True)
        if not self.cfg.unlock_code:
            log.warning("GLNK_UNLOCK_CODE is empty: the 'Open door' button will not work")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("MQTT connected to %s:%d", self.cfg.mqtt_host, self.cfg.mqtt_port)
        self._discover()
        client.publish(self.base + "/status", "online", retain=True)
        client.publish(self.base + "/ringing", "OFF", retain=True)
        self._publish_history()
        client.subscribe([(self.base + "/unlock/set", 1), (self.base + "/chime/set", 1),
                          (self.base + "/test_ring/set", 1)])

    def _on_message(self, client, userdata, msg):
        action = msg.topic.rsplit("/", 2)[-2]
        log.info("command: %s", action)
        target = {"unlock": self.unlock, "chime": self.play_chime, "test_ring": self.simulate_ring}.get(action)
        if target:
            threading.Thread(target=target, daemon=True).start()

    # ------------------------------------------------------------------ ring
    def on_snapshot(self, path):
        """Called by the FTP server for every uploaded file."""
        if not path.lower().endswith((".jpg", ".jpeg")):
            log.info("ignored upload %s", path)
            return
        try:
            shutil.copyfile(path, self.last_jpg + ".tmp")
            os.replace(self.last_jpg + ".tmp", self.last_jpg)
            with open(self.last_jpg, "rb") as f:
                self.mqtt.publish(self.base + "/snapshot", f.read(), retain=True)
        except OSError as e:
            log.error("snapshot copy failed: %s", e)
        self._ring(source=os.path.basename(path), picture=path)

    def _ring(self, source, picture=None):
        now = time.monotonic()
        new = now - self.last_ring > self.cfg.ring_gap
        self.last_ring = now
        if picture:
            self._add_to_history(picture, new)
        if new:
            log.info("RING (%s)", source)
            self.mqtt.publish(self.base + "/ringing", "ON", retain=True)
            self.mqtt.publish(self.base + "/last_ring",
                              datetime.now(timezone.utc).isoformat(timespec="seconds"), retain=True)
            self.mqtt.publish(self.base + "/event", json.dumps({"event_type": "ring"}))
        if self.ring_off_timer:
            self.ring_off_timer.cancel()
        self.ring_off_timer = threading.Timer(self.cfg.ring_hold, self._ring_off)
        self.ring_off_timer.daemon = True
        self.ring_off_timer.start()

    # ------------------------------------------------------------------ visitor history
    def _load_history(self):
        try:
            with open(self.hist_file) as f:
                items = json.load(f)
            return [h for h in items if os.path.exists(os.path.join(self.hist_dir, h["file"]))]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _import_history(self):
        """First start with history enabled: rebuild the last visitors from the FTP pictures still on
        disk (same grouping as live rings: pictures closer than RING_GAP are one ring)."""
        if not self.cfg.history_size:
            return
        pics = []
        for root, _, files in os.walk(self.ftp_dir):
            for name in files:
                if name.lower().endswith((".jpg", ".jpeg")):
                    p = os.path.join(root, name)
                    try:
                        pics.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
        pics.sort()
        rings = []                                   # [(time of first picture, last picture)]
        for t, p in pics:
            if rings and t - rings[-1][2] <= self.cfg.ring_gap:
                rings[-1] = (rings[-1][0], p, t)
            else:
                rings.append((t, p, t))
        for t, p, _ in reversed(rings[-self.cfg.history_size:]):
            stamp = datetime.fromtimestamp(t, timezone.utc)
            entry = {"time": stamp.isoformat(timespec="seconds"),
                     "file": stamp.strftime("ring_%Y%m%d_%H%M%S.jpg")}
            try:
                shutil.copyfile(p, os.path.join(self.hist_dir, entry["file"]))
                self.history.append(entry)
            except OSError as e:
                log.error("history import failed for %s: %s", p, e)
        try:
            with open(self.hist_file + ".tmp", "w") as f:
                json.dump(self.history, f)
            os.replace(self.hist_file + ".tmp", self.hist_file)
        except OSError as e:
            log.error("history save failed: %s", e)
        if self.history:
            log.info("visitor history rebuilt from %d FTP picture(s): %d ring(s)", len(pics), len(self.history))

    def _add_to_history(self, picture, new_ring):
        """A new ring opens a history entry; later pictures of the same burst replace its picture,
        so each entry shows the last picture of its ring (like the Visitor image)."""
        if not self.cfg.history_size:
            return
        with self.hist_lock:
            if new_ring or not self.history:
                stamp = datetime.now(timezone.utc)
                entry = {"time": stamp.isoformat(timespec="seconds"),
                         "file": stamp.strftime("ring_%Y%m%d_%H%M%S.jpg")}
                self.history.insert(0, entry)
                for old in self.history[self.cfg.history_size:]:
                    try:
                        os.remove(os.path.join(self.hist_dir, old["file"]))
                    except OSError:
                        pass
                del self.history[self.cfg.history_size:]
            dest = os.path.join(self.hist_dir, self.history[0]["file"])
            try:
                shutil.copyfile(picture, dest + ".tmp")
                os.replace(dest + ".tmp", dest)
                with open(self.hist_file + ".tmp", "w") as f:
                    json.dump(self.history, f)
                os.replace(self.hist_file + ".tmp", self.hist_file)
            except OSError as e:
                log.error("history update failed: %s", e)
            self._publish_history(only_first=not new_ring)

    def _publish_history(self, only_first=False):
        slots = 1 if only_first else self.cfg.history_size
        for i in range(slots):
            topic = "%s/history/%d/" % (self.base, i + 1)
            if i < len(self.history):
                try:
                    with open(os.path.join(self.hist_dir, self.history[i]["file"]), "rb") as f:
                        self.mqtt.publish(topic + "image", f.read(), retain=True)
                except OSError as e:
                    log.error("history picture missing: %s", e)
                self.mqtt.publish(topic + "time", self.history[i]["time"], retain=True)
            else:
                self.mqtt.publish(topic + "time", "None", retain=True)     # "None" = unknown in HA

    def _ring_off(self):
        self.mqtt.publish(self.base + "/ringing", "OFF", retain=True)

    def simulate_ring(self):
        if os.path.exists(self.last_jpg):
            with open(self.last_jpg, "rb") as f:
                self.mqtt.publish(self.base + "/snapshot", f.read(), retain=True)
        self.last_ring = 0.0
        self._ring(source="simulated")

    # ------------------------------------------------------------------ device actions
    def _session(self):
        return GlnkClient(self.cfg.glnk_host, self.cfg.glnk_port, self.cfg.glnk_user, self.cfg.glnk_password)

    def unlock(self):
        if not self.cfg.unlock_code:
            self.mqtt.publish(self.base + "/unlock/state", "error: no unlock code configured", retain=True)
            return
        with self.device_lock:
            c = self._session()
            try:
                c.connect()
                ok = c.unlock(self.cfg.unlock_code, self.cfg.unlock_delay)
                state = "opened" if ok else "refused by the device"
            except (GlnkError, OSError) as e:
                state = "error: %s" % e
            finally:
                c.close()
        log.info("unlock: %s", state)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.mqtt.publish(self.base + "/unlock/state", "%s (%s)" % (state, stamp), retain=True)

    def play_chime(self):
        with self.device_lock:
            c = self._session()
            try:
                c.connect()
                c.talk_start()
                audio = chime()
                start = time.monotonic()
                for n in range(0, len(audio), 320):
                    c.talk_send(audio[n:n + 320])
                    c.pump(max(0.0, start + (n // 320 + 1) * 0.04 - time.monotonic()))
                c.talk_stop()
                c.pump(0.2)
                log.info("chime played")
            except (GlnkError, OSError) as e:
                log.error("chime failed: %s", e)
            finally:
                c.close()

    # ------------------------------------------------------------------ housekeeping
    def cleanup_loop(self):
        while True:
            limit = time.time() - self.cfg.keep_days * 86400
            for root, dirs, files in os.walk(self.ftp_dir, topdown=False):
                for name in files:
                    p = os.path.join(root, name)
                    try:
                        if os.path.getmtime(p) < limit:
                            os.remove(p)
                    except OSError:
                        pass
                if root != self.ftp_dir and not os.listdir(root):
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
            time.sleep(3600)

    # ------------------------------------------------------------------ FTP
    def ftp_server(self):
        bridge, cfg = self, self.cfg

        class Handler(FTPHandler):
            def on_connect(self):
                if cfg.ftp_allowed_ip and self.remote_ip != cfg.ftp_allowed_ip:
                    log.warning("FTP connection refused from %s", self.remote_ip)
                    self.close()

            def on_login_failed(self, username, password):
                # usually a FTP_USER / FTP_PASSWORD mismatch with the doorbell web UI
                log.warning("FTP login failed for user %r from %s", username, self.remote_ip)

            def on_incomplete_file_received(self, file):
                log.warning("FTP upload interrupted: %s", os.path.basename(file))

            def on_file_received(self, file):
                log.debug("FTP upload: %s", file)
                bridge.on_snapshot(file)

        auth = DummyAuthorizer()
        auth.add_user(cfg.ftp_user, cfg.ftp_password, self.ftp_dir, perm="emw")   # cd, mkdir, write
        Handler.authorizer = auth
        Handler.passive_ports = cfg.ftp_passive
        Handler.banner = "glnk-bridge"
        logging.getLogger("pyftpdlib").setLevel(logging.WARNING)
        server = FTPServer((cfg.ftp_bind, cfg.ftp_port), Handler)
        server.max_cons = 8
        log.info("FTP server listening on %s:%d (allowed: %s)", cfg.ftp_bind, cfg.ftp_port,
                 cfg.ftp_allowed_ip or "anyone")
        return server

    # ------------------------------------------------------------------ main
    def run(self):
        server = self.ftp_server()
        threading.Thread(target=self.cleanup_loop, daemon=True).start()
        self.mqtt.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        self.mqtt.loop_start()

        def stop(*_):
            log.info("stopping")
            self.mqtt.publish(self.base + "/status", "offline", retain=True).wait_for_publish(2)
            self.mqtt.loop_stop()
            server.close_all()
            sys.exit(0)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(handle_exit=False)


def main():
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Bridge(Config()).run()
