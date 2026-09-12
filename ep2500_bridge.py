#!/usr/bin/env python3
"""
EP2500 <-> MQTT bridge with Home Assistant discovery and a zero-export
controller.

What it does:
  1. Holds a persistent local Tuya connection to the Oukitel EP2500
     (protocol 3.5). After the first full status the device only sends delta
     frames, so a cache is kept and every frame is merged into it.
  2. Publishes everything relevant over MQTT with HA discovery.
  3. Control loop: trims DP 121 (export limit) so the Eco Tracker meter
     reading moves towards the configured target.

Configured through environment variables; defaults below.

Dependencies:  pip install tinytuya paho-mqtt
"""

import datetime
import json
import logging
import math
import signal
import os
import queue
import threading
import time
import urllib.request
import http.server
import socketserver
import re
import shutil

import paho.mqtt.client as mqtt
import tinytuya

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEVICE_ID = os.getenv("EP2500_ID", "")
DEVICE_IP = os.getenv("EP2500_IP", "")
LOCAL_KEY = os.getenv("EP2500_KEY", "")
DEVICE_PORT = int(os.getenv("EP2500_PORT", "6668"))

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")

BASE = "ep2500"
DISC = "homeassistant"

# Meter reading (positive = importing, negative = exporting).
# GRID_SOURCE "http"  -> poll the Eco Tracker directly (default)
# GRID_SOURCE "mqtt"  -> take the value from GRID_TOPIC
GRID_SOURCE = os.getenv("GRID_SOURCE", "http").lower()

ECO_URL = os.getenv("ECO_URL", "http://192.168.1.100/status")
ECO_FIELD = os.getenv("ECO_FIELD", "power")        # oder "powerAvg"
ECO_INTERVAL = int(os.getenv("ECO_INTERVAL", "5"))  # s

# Plausibility check: a jump larger than ECO_SPIKE watts against the previous
# reading is only accepted once the next reading confirms it. That catches
# single outliers which would otherwise drive the controller to its limit.
# 0 turns the check off.
ECO_SPIKE = int(os.getenv("ECO_SPIKE", "600"))

# Metering Shelly in front of the device - on independent check on the actual
# AC power, and a hard on/off switch.
SHELLY_IP = os.getenv("SHELLY_IP", "")
SHELLY_INTERVAL = int(os.getenv("SHELLY_INTERVAL", "5"))

# Second Shelly, for example on a north-facing balcony PV system. Leave empty
# if you do not have one.
SHELLY2_IP = os.getenv("SHELLY2_IP", "")

# Optional Shelly plugs in front of up to two AC-coupled storage units. Their
# charging power is removed from a negative meter target so the EP2500 does
# not keep increasing its output while the other storage absorbs the surplus.
AC_STORAGE1_IP = os.getenv("AC_STORAGE1_IP", "")
AC_STORAGE2_IP = os.getenv("AC_STORAGE2_IP", "")
AC_STORAGE_MAX_AGE = float(os.getenv("AC_STORAGE_MAX_AGE", "15"))
AC_STORAGE_NOISE = float(os.getenv("AC_STORAGE_NOISE", "5"))

# Usable capacity for the runtime estimate. Per the type plate,
# 51.2 V x 40 Ah = 2048 Wh.
BATT_WH = int(os.getenv("BATT_WH", "2048"))

# Metering Shelly in front of the device, used as on independent cross-check
# against the EP2500's AC output power. The address is editable from HA.

# Second Shelly, for example on a north-facing balcony PV system. Leave empty
# if you do not have one.

ECO_ABSURD = 30000          # W; beyond this it is not a reading

GRID_TOPIC = os.getenv("GRID_TOPIC", "ecotracker/power")
GRID_JSON_KEY = os.getenv("GRID_JSON_KEY", "power")
GRID_MAX_AGE = 60          # s; older meter readings count as invalid

# Desired power at the grid meter. Positive values deliberately keep a small
# grid import, negative values deliberately export. The latter is useful when
# another AC-coupled storage unit should see surplus and start charging.
METER_TARGET_MIN = int(os.getenv("METER_TARGET_MIN", "-2000"))
METER_TARGET_MAX = int(os.getenv("METER_TARGET_MAX", "2000"))
if METER_TARGET_MIN > METER_TARGET_MAX:
    METER_TARGET_MIN, METER_TARGET_MAX = METER_TARGET_MAX, METER_TARGET_MIN

DP_LIMIT = "121"           # export limit, writable
DP_CHARGE = "122"          # total battery charge limit, writable
DP_AC_OUT = "155"          # actual AC output power
DP_BATT = "128"            # battery power, positive = charging
DP_OFFGRID = "119"         # off-grid socket on/off, writable
DP_BACKFLOW = "118"        # backflow prevention: True = no export
DP_SOC_MAX = "123"         # charge-stop SoC in %
DP_SOC_MIN = "124"         # discharge-stop SoC in %
DP_PV_LIMIT = "156"        # PV charge limit, writable

LIMIT_MIN = 0
LIMIT_MAX = int(os.getenv("LIMIT_MAX", "800"))   # legal ceiling
LIMIT_SAFE = int(os.getenv("LIMIT_SAFE", "0"))   # fallback on shutdown
# Export limit to fall back to when the meter has been silent for longer
# than GRID_MAX_AGE. 0 stops export entirely; a positive value keeps feeding
# blind. The battery charge limit is untouched so PV can keep charging.
GRID_FAIL_LIMIT = int(os.getenv("GRID_FAIL_LIMIT", "0"))
# Ceiling for the battery charge limit (DP 122). Per the type plate the device
# takes up to 4000 W across its four MPPT inputs; the battery itself caps at
# 60 A, around 3000 W. 2500 W is the app's default and covers typical
# installations.
CHARGE_HW_MAX = int(os.getenv("CHARGE_HW_MAX", "4000"))

# Operating mode. DP 117 reports what is in effect and is read-only; the five
# schedule slots 165-169 are writable. Measured on this unit: writing DP 165
# pulls DP 117 along one second later, in both directions. DP 169 ("other
# time") is written too so the app view stays consistent, but it is 165 that
# decides.
#
# In backup_power the device charges from the grid at DP 122, unconditionally
# and regardless of the meter. That is the opposite of zero-export control, so
# the controller has to be out of the way whenever this mode is active.
DP_MODE = "117"
DP_MODE_SLOT1 = "165"
DP_MODE_SLOT5 = "169"
MODE_GRID = "grid_priority"
MODE_BACKUP = "backup_power"
MODE_VERIFY_DELAY = 8      # s before reading DP 117 back

BACKUP_POWER_DEFAULT = int(os.getenv("BACKUP_POWER", "800"))

# AC surplus charging. The EP2500 absorbs surplus that nothing else takes.
#
# The hard part is telling real surplus from surplus another storage unit is
# producing. Measured on 12.09.: with the EP2500 drawing 800 W the meter read
# only +146 W, because the neighbouring unit discharged to cover it. The
# moment the EP2500 stopped, the meter jumped to -639 W. Both controllers held
# the meter near zero while one battery emptied into the other.
#
# The meter alone cannot tell those apart. NEIGHBOUR_TOPIC therefore carries
# the neighbouring battery's power; while it discharges, no surplus is taken.
# Phase readings. The Eco Tracker reports the balanced sum and each phase
# separately. The zero-export controller keeps using ECO_FIELD exactly as
# before - it is proven and averaged. The surplus logic gets its own set of
# instantaneous values with one common smoothing filter, so it never compares
# an averaged sum against an unaveraged phase.
PHASE_SMOOTH = float(os.getenv("PHASE_SMOOTH", "0.25"))   # EMA weight, 0..1
PHASE_FIELDS = ("powerPhase1", "powerPhase2", "powerPhase3")
# How often the phases land in the log. They go to MQTT continuously, but the
# recordings are what you read afterwards when working out why the surplus
# logic did what it did - and without the phases in there, the deciding
# numbers are missing. 0 turns it off.
PHASE_LOG_INTERVAL = int(os.getenv("PHASE_LOG_INTERVAL", "60"))

# Telling real surplus from surplus a neighbouring battery is producing cannot
# be done from meter readings alone - a phase reading negative looks the same
# either way. What does work is checking whether the meter follows our own
# change: raise the charge limit by one step, wait, and see whether the sum
# moved with it. If something else compensated, the sum stays put.
#
# Every increase is therefore a probe, and only increases are. Coming down is
# always allowed and immediate. That makes the loop asymmetric in the safe
# direction and deliberately unhurried.
# Measured on 12.09. against a Hoymiles 4020X on another phase: 19 s after
# the EP2500 started drawing 741 W the neighbour had not reacted at all and
# 74 % of the step still showed at the meter. After 79 s it covered 640 W and
# only 14 % was left. A verdict at 25 s would therefore have read "genuine"
# and walked straight into the loop it exists to prevent. The window has to
# outlast the neighbour's response, not the device's own.
PROBE_SETTLE = int(os.getenv("PROBE_SETTLE", "90"))   # s before judging a step
PROBE_ACCEPT = float(os.getenv("PROBE_ACCEPT", "0.6"))  # share the meter must follow
PROBE_MIN_DRAW = int(os.getenv("PROBE_MIN_DRAW", "60"))  # W; below this no verdict

SURPLUS_ENTER_W = int(os.getenv("SURPLUS_ENTER_W", "100"))
SURPLUS_ENTER_S = int(os.getenv("SURPLUS_ENTER_S", "120"))
SURPLUS_EXIT_S = int(os.getenv("SURPLUS_EXIT_S", "90"))
SURPLUS_MIN_W = int(os.getenv("SURPLUS_MIN_W", "100"))
SURPLUS_STEP = int(os.getenv("SURPLUS_STEP", "200"))
NEIGHBOUR_TOPIC = os.getenv("NEIGHBOUR_TOPIC", "")
# JSON field inside that topic, empty when the payload is a bare number.
NEIGHBOUR_FIELD = os.getenv("NEIGHBOUR_FIELD", "bat_p")
# Sign of the neighbour reading. "discharge_positive" matches the Hoymiles
# bat_p field; use "charge_positive" if yours is the other way round.
NEIGHBOUR_SIGN = os.getenv("NEIGHBOUR_SIGN", "discharge_positive")
NEIGHBOUR_DISCHARGE_W = int(os.getenv("NEIGHBOUR_DISCHARGE_W", "40"))
NEIGHBOUR_MAX_AGE = int(os.getenv("NEIGHBOUR_MAX_AGE", "60"))

# Controller parameters. Starting values; adjustable from HA at runtime and
# stored as retained MQTT, so they survive a restart of the bridge.
# TUNABLES: key -> (display name, min, max, step, unit, type)
TUNABLES = {
    "interval": ("Control interval",   3,   120, 1,   "s", int),
    "gain":     ("Controller gain", 0.1, 2.0, 0.1, None, float),
    "deadband": ("Deadband",          0,   200, 5,   "W", int),
    "max_step": ("Max step", 10,  800, 10,  "W", int),
    "charge_limit": ("Charge limit guard", 0, CHARGE_HW_MAX, 100, "W", int),
    "soc_pass":  ("Pass-through from SoC", 50, 100, 1, "%", int),
    "pv_max":    ("PV limit when open", 500, 4000, 100, "W", int),
}

TUNE_DEFAULTS = {
    "interval": 6,
    "gain": 1.0,
    "deadband": 15,
    "max_step": 800,
    "charge_limit": 2500,
    "soc_pass": 95,
    "pv_max": 4000,
}

CTRL_MIN_STEP = 10         # W; smaller changes are not written
WINDUP_MARGIN = 60         # W; how far the setpoint may sit above actual

# Pass-through: the EP2500 has no direct path from PV to the grid - export
# always comes out of the battery, and PV always goes into it. A steady state
# of charge therefore just means PV charge power equals export power. Both
# values are set once and only trimmed slowly afterwards.
#
# The control variable is the state of charge, not battery power. The state of
# charge is exactly what should be held, and it moves slowly. Battery power
# arrives 20 to 70 s late - using it as the control variable makes the loop
# swing from one limit to the other.
PASS_HYST = 5              # points below target where pass-through ends
PASS_STEP = 25             # W per ordinary trim step
PASS_STEP_FAST = 150       # W per step in the alarm range
PASS_ADJUST = 180          # s between two trim steps
PASS_ADJUST_FAST = 45      # s in the alarm range
PASS_ALARM = 3             # points above target = alarm range
# A negative meter target deliberately exports, which conflicts with
# pass-through pinning the export limit. Pass-through therefore stays out of
# the way - but only until the state of charge gets close enough to the charge
# stop that the 100 % shutdown becomes the bigger problem. From this many
# points above the pass-through target, protection wins over the target.
PASS_OVERRIDE = int(os.getenv("PASS_OVERRIDE", "3"))
PASS_SETTLE = 120          # s of quiet after start before trimming

# How long the device must have been out of standby before control resumes.
# On on empty battery in weak sun the EP2500 flips back and forth within
# seconds - nine times in 46 minutes on 10.09. Without debouncing the event
# log consists of nothing but those transitions.
IDLE_HOLD = 180            # s
PASS_BATT_BIAS = 50        # W per point of deviation, target for the gate
PASS_PV_CAP = 2 * LIMIT_MAX  # W; pass-through never needs more

# The device reports some values as unsigned 16-bit numbers. 65535 is then not
# a power reading but -1. Above this threshold the value is converted back;
# anything beyond PLAUSIBLE_MAX afterwards counts as invalid and is discarded, so
# neither the display nor the energy counters get corrupted.
UINT16_SCHWELLE = 32768
PLAUSIBLE_MAX = {
    "143": 5000,    # PV total, device takes 4000 W max
    "147": 1200, "151": 1200, "164": 1200, "180": 1200,   # je String max 1000 W
    "155": 3000,    # AC-Ausgang
    "128": 3000,    # battery power
    "137": 3000,    # Netzleistung
    "141": 3000,    # Off-Grid-Last
}
# Only these may sensibly be negative (the sign carries the direction).
# PV power can never be negative - a negative value there is on overflow or a
# measurement error and gets discarded.
NEGATIVE_OK = {"128", "137"}

POLL_FULL = 60             # s between two full status polls
VERIFY_DELAY = 6           # s before a switch command is read back

# DP 135 is NOT a state readout for the off-grid socket. Across three
# measurement runs the value never left the 231 to 237 V band - overnight in
# standby, with the output switched off, and on on empty battery alike. It
# appears to report on internal bus voltage rather than the socket. A watchdog
# built on it produced nothing but false alarms and was removed again.

# The device only releases the off-grid output once the state of charge is
# this far above the discharge stop. Below that it accepts a write to DP 119
# without complaint and leaves the output dead. Measured on 11.09.: discharge
# stop at 15 %, output and control both came back at 20 %.
OFFGRID_SOC_MARGIN = int(os.getenv("OFFGRID_SOC_MARGIN", "5"))
HEARTBEAT = 9              # s
TUYA_RECEIVE_TIMEOUT = float(os.getenv("TUYA_RECEIVE_TIMEOUT", "1"))
TUYA_COMMAND_TIMEOUT = float(os.getenv("TUYA_COMMAND_TIMEOUT", "12"))

# Log changes to individual datapoints. To identify unknown DPs: set
# LOG_DPS=1, change a setting in the app, and check the log for which
# datapoint moved.
#   LOG_DPS=0   off (default)
#   LOG_DPS=1   only DPs not yet mapped (readable)
#   LOG_DPS=2   every DP including measurements (very chatty)
LOG_DPS = int(os.getenv("LOG_DPS", "0"))

# Datapoints already mapped - used only to label the log.
DP_NAMES = {
    "102": "SoC", "117": "Mode", "118": "Backflow prevention",
    "119": "Off-grid socket", "120": "Anti-backflow",
    "121": "Export limit", "122": "Battery charge limit",
    "123": "SoC max", "124": "SoC min",
    "125": "PV energy today", "126": "PV energy total",
    "127": "Batt voltage", "128": "Batt power",
    "130": "Cell max", "131": "Cell min",
    "132": "Temperature 1", "133": "Temperature 2",
    "134": "Status", "135": "Off-grid voltage", "136": "Grid current",
    "137": "Grid power", "138": "Grid frequency", "139": "Grid voltage",
    "140": "Off-grid current", "141": "Off-grid load", "142": "Off-grid load (2)",
    "143": "PV total", "147": "PV1-P", "148": "PV1-U", "150": "PV1-I",
    "151": "PV3-P", "155": "AC output", "156": "PV limit",
    "163": "PV3-I", "164": "PV2-P", "178": "PV2-U", "179": "PV2-I",
    "180": "PV4-P", "181": "PV4-U", "182": "PV4-I", "183": "PV3-U",
    "149": "System fault",
    "107": "HW main control", "108": "SW main control",
    "109": "HW inverter", "110": "SW inverter", "184": "SW inverter PV",
    "112": "SW BMS", "113": "HW WiFi", "114": "SW WiFi",
    "115": "Meter serial", "145": "OTA URL",
    "152": "WiFi SSID (meter)", "153": "WiFi password (meter)",
    "154": "Network switch",
}

# Readings that change constantly - not interesting in LOG_DPS=1 mode.
DP_NOISY = {"102", "125", "126", "127", "128", "135", "136", "137", "138",
            "139", "142", "143", "147", "148", "150", "151", "155", "163",
            "164", "178", "179", "180", "181", "182", "183"}


def is_standby(status):
    """Detects the standby state from DP 134.

    The firmware writes "standy_status" (no b). Other versions might report
    "standby" or something similar, so only the word stem is checked.
    """
    return "stand" in str(status).lower()


def report_fault(value):
    """Raises on event when the system fault register (DP 149) changes.

    The harmless flicker between 0 and 2 triggers nothing, and no all-clear is
    reported retroactively when the bridge starts.
    """
    before = st.fault_reported
    if value == before:
        return
    st.fault_reported = value
    text, serious = fault_text(value)
    if serious:
        event(f"System fault {value}: {text} - latched until the device is "
              f"restarted")
    elif before is not None and fault_text(before)[1]:
        event("System fault cleared")


def log_changes(changed, source=""):
    """Logs changed datapoints and checks the fault register.

    The fault check deliberately sits before the LOG_DPS gate. It is
    monitoring rather than logging, so it has to run even when DP logging is
    switched off.
    """
    if not changed:
        return
    if "149" in changed:
        report_fault(changed["149"][1])
    if not LOG_DPS:
        return
    for dp, (old, new) in sorted(changed.items(), key=lambda x: int(x[0])):
        if LOG_DPS == 1 and dp in DP_NOISY:
            continue
        name = DP_NAMES.get(dp, "?")
        log.info("DP %-4s %-22s %r -> %r %s", dp, name, old, new, source)


EVENT_MAX_AGE = 48 * 3600   # s; older events are dropped
EVENT_MAX = 200             # safety cap on the attribute size


def event(text, also_log=True):
    """Records on event for display in Home Assistant."""
    now_ts = time.time()
    with st.lock:
        st.events.append({
            "ts": int(now_ts),
            "t": datetime.datetime.fromtimestamp(now_ts).strftime("%d.%m. %H:%M:%S"),
            "m": text[:120],
        })
        st.events = [e for e in st.events if now_ts - e["ts"] <= EVENT_MAX_AGE
                     ][-EVENT_MAX:]
        entries = list(st.events)
    if also_log:
        log.info("%s", text)
    try:
        mqttc.publish(f"{BASE}/events", json.dumps({
            "last": text[:250],
            "count": len(entries),
            "entries": entries,
        }), retain=True)
    except Exception as exc:
        log.debug("Could not publish event: %s", exc)

logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ep2500")


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
# A switch on the dashboard makes the bridge mirror everything it logs into a
# file as well. For downloading, the bridge brings its own small HTTP server -
# that works regardless of whether Home Assistant runs on the same machine and
# needs no shared directory.
REC_DIR = os.getenv("REC_DIR", "/data/recordings")
REC_PORT = int(os.getenv("REC_PORT", "8099"))
REC_MAX_MB = int(os.getenv("REC_MAX_MB", "50"))   # per file
REC_KEEP = int(os.getenv("REC_KEEP", "10"))       # delete older files
# Address the bridge is reachable at on the home network. Used only to build
# the dashboard link - the server itself listens on all addresses.
REC_HOST = os.getenv("REC_HOST", "")


class RecordHandler(logging.Handler):
    """Writes log lines to a file while a recording is running.

    The file is flushed after every line, so downloading during a recording
    gives the current state and a crash swallows nothing.
    """

    def __init__(self):
        super().__init__()
        self.file_lock = threading.Lock()
        self.fh = None
        self.file_name = None
        self.lines = 0
        self.started_at = None
        self.aborted = None      # reason a recording ended by itself

    def emit(self, record):
        with self.file_lock:
            if self.fh is None:
                return
            try:
                self.fh.write(self.format(record) + "\n")
                self.fh.flush()
                self.lines += 1
                if self.lines % 500 == 0 and self._too_big():
                    self._stop_intern(f"size limit of {REC_MAX_MB} MB reached")
            except Exception:
                pass          # a broken recording must never disturb control

    def _too_big(self):
        try:
            return self.fh.tell() > REC_MAX_MB * 1024 * 1024
        except Exception:
            return False

    def _stop_intern(self, reason_txt=""):
        # The reason is only stored here, never reported. This runs inside
        # emit() while the file lock is held, and event() would log again -
        # straight back into emit(). publish_record_info() picks it up.
        if reason_txt:
            self.aborted = reason_txt
        if self.fh is not None:
            try:
                self.fh.close()
            except Exception:
                pass
        self.fh = None

    def take_abort_reason(self):
        """Returns and clears the reason a recording ended on its own."""
        with self.file_lock:
            reason_txt, self.aborted = self.aborted, None
            return reason_txt

    def start(self):
        """Starts a new recording. Returns the file name, or None."""
        with self.file_lock:
            if self.fh is not None:
                return self.file_name
            try:
                os.makedirs(REC_DIR, exist_ok=True)
                name = time.strftime("ep2500_%Y%m%d_%H%M%S.log")
                self.fh = open(os.path.join(REC_DIR, name), "w",
                                  encoding="utf-8")
                self.file_name = name
                self.lines = 0
                self.started_at = time.time()
            except Exception as exc:
                log.error("Recording could not start: %s", exc)
                self.fh = None
                return None
        prune_recordings()
        return self.file_name

    def stop(self):
        with self.file_lock:
            self._stop_intern()
            return self.file_name

    def running(self):
        return self.fh is not None

    def info(self):
        with self.file_lock:
            size = 0
            if self.fh is not None:
                try:
                    size = self.fh.tell()
                except Exception:
                    pass
            return {
                "active": self.fh is not None,
                "file": self.file_name,
                "lines": self.lines,
                "kib": round(size / 1024, 1),
                "minutes": (round((time.time() - self.started_at) / 60, 1)
                              if self.started_at and self.fh is not None else None),
            }


recorder = RecordHandler()
recorder.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(recorder)


def prune_recordings():
    """Deletes old recordings so the disk does not fill up."""
    try:
        files = sorted(f for f in os.listdir(REC_DIR)
                         if f.startswith("ep2500_") and f.endswith(".log"))
        for f in files[:-REC_KEEP]:
            os.remove(os.path.join(REC_DIR, f))
            log.info("Old recording deleted: %s", f)
    except Exception as exc:
        log.warning("Cleanup failed: %s", exc)


class RecordServer(http.server.BaseHTTPRequestHandler):
    """Lists the recordings and serves them for download."""

    def log_message(self, *args):
        pass              # do not write to our own log, that would loop

    def do_GET(self):
        path_ = self.path.split("?")[0]
        if path_ in ("/", "/index.html"):
            return self._index()
        name = path_.lstrip("/")
        # Allow only our own file names - no ../ and no arbitrary path. The
        # server runs without authentication on the home network.
        if not re.fullmatch(r"ep2500_\d{8}_\d{6}\.log", name):
            self.send_error(404)
            return
        full = os.path.join(REC_DIR, name)
        if not os.path.isfile(full):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(os.path.getsize(full)))
        self.end_headers()
        with open(full, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    def _index(self):
        try:
            files = sorted((f for f in os.listdir(REC_DIR)
                              if f.startswith("ep2500_") and f.endswith(".log")),
                             reverse=True)
        except Exception:
            files = []
        lines = ["<!doctype html><meta charset='utf-8'>",
                  "<title>EP2500 recordings</title>",
                  "<h1>EP2500 recordings</h1>"]
        if not files:
            lines.append("<p>No recordings yet.</p>")
        else:
            lines.append("<ul>")
            for f in files:
                kib = os.path.getsize(os.path.join(REC_DIR, f)) / 1024
                active = " (running)" if f == recorder.file_name and recorder.running() else ""
                lines.append(f"<li><a href='/{f}'>{f}</a> &ndash; "
                              f"{kib:.0f} kB{active}</li>")
            lines.append("</ul>")
        body = "\n".join(lines).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LeiserServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def record_info_loop():
    """Keeps the recording status display up to date."""
    while True:
        time.sleep(30)
        try:
            publish_record_info()
        except Exception:
            pass


def record_server_loop():
    try:
        with LeiserServer(("", REC_PORT), RecordServer) as srv:
            log.info("Recordings available on port %s", REC_PORT)
            srv.serve_forever()
    except Exception as exc:
        log.error("Recording server did not start: %s", exc)

# --------------------------------------------------------------------------
# Entities for HA discovery
# --------------------------------------------------------------------------
# (dp, key, display name, unit, device_class, state_class, factor)

SENSORS = [
    ("102", "soc",           "State of charge",           "%",   "battery",     "measurement", 1),
    ("155", "ac_out",        "AC output power", "W",   "power",       "measurement", 1),
    ("128", "batt_power",    "Battery power",    "W",   "power",       "measurement", 1),
    ("137", "grid_power",    "Grid power",        "W",   "power",       "measurement", 1),
    ("143", "pv_power",      "PV power",         "W",   "power",       "measurement", 1),
    ("125", "pv_energy",     "PV energy today",     "Wh",  "energy", "total_increasing", 1),
    ("126", "pv_energy_all", "PV energy total",    "Wh",  "energy", "total_increasing", 1),
    ("127", "batt_voltage",  "Battery voltage",    "V",   "voltage",     "measurement", 0.01),
    ("139", "grid_voltage",  "Grid voltage",        "V",   "voltage",     "measurement", 0.1),
    ("138", "grid_freq",     "Grid frequency",        "Hz",  "frequency",   "measurement", 0.01),
    ("136", "grid_current",  "Grid current",           "A",   "current",     "measurement", 0.1),
    ("134", "status",        "Status",              None,  None,          None,          None),
    ("117", "mode",          "Operating mode",       None,  None,          None,          None),
    # Four MPPT strings. Power, voltage and current are not stored
    # contiguously in the device; the mapping was verified against the app.
    ("147", "pv1_power",     "PV1 power",        "W",   "power",       "measurement", 1),
    ("148", "pv1_voltage",   "PV1 voltage",        "V",   "voltage",     "measurement", 0.1),
    ("150", "pv1_current",   "PV1 current",           "A",   "current",     "measurement", 0.01),
    ("164", "pv2_power",     "PV2 power",        "W",   "power",       "measurement", 1),
    ("178", "pv2_voltage",   "PV2 voltage",        "V",   "voltage",     "measurement", 0.1),
    ("179", "pv2_current",   "PV2 current",           "A",   "current",     "measurement", 0.01),
    ("151", "pv3_power",     "PV3 power",        "W",   "power",       "measurement", 1),
    ("183", "pv3_voltage",   "PV3 voltage",        "V",   "voltage",     "measurement", 0.1),
    ("163", "pv3_current",   "PV3 current",           "A",   "current",     "measurement", 0.01),
    ("180", "pv4_power",     "PV4 power",        "W",   "power",       "measurement", 1),
    ("181", "pv4_voltage",   "PV4 voltage",        "V",   "voltage",     "measurement", 0.1),
    ("182", "pv4_current",   "PV4 current",           "A",   "current",     "measurement", 0.01),
    # Battery internals (16S2P per the type plate)
    ("130", "cell_max",      "Cell voltage max",    "mV",  "voltage",     "measurement", 1),
    ("131", "cell_min",      "Cell voltage min",    "mV",  "voltage",     "measurement", 1),
    ("132", "temp1",         "Temperature 1",        "°C",  "temperature", "measurement", 0.1),
    ("133", "temp2",         "Temperature 2",        "°C",  "temperature", "measurement", 0.1),
    ("140", "offgrid_curr",  "Off-grid current",      "A",   "current",     "measurement", 0.1),
    ("156", "pv_limit",      "PV charge limit",       "W",   "power",       "measurement", 1),
    # Off-grid socket: runs on a separate path and is NOT included in the AC
    # output (155). DP 142 carries the same value.
    ("141", "offgrid_power", "Off-grid load",       "W",   "power",       "measurement", 1),
    ("135", "offgrid_volt",  "Off-grid voltage",   "V",   "voltage",     "measurement", 0.1),
]

DEVICE_INFO = {
    "identifiers": [f"ep2500_{DEVICE_ID}"],
    "name": "Oukitel EP2500",
    "manufacturer": "Oukitel",
    "model": "WXY001",
}


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.dps = {}              # cache of every datapoint seen
        self.grid = None           # last meter reading in W
        self.grid_ts = 0.0
        self.grid_suspect = None  # jump value awaiting confirmation
        self.control_on = False    # control active?
        self.correction = 0        # target at the meter, in W
        self.last_write = 0.0
        self.online = False
        self.tune = dict(TUNE_DEFAULTS)
        self.restored = set()      # which values already came back from MQTT
        self.idle_logged = False   # idle lock already reported?
        self.idle_since = 0.0      # last seen idle
        self.offgrid_reported = False
        self.fault_reported = None  # DP 149 value last reported as an event
        self.pass_on = False       # pass-through enabled?
        self.pass_active = False   # pass-through running right now?
        self.pass_pv_setpoint = 0      # W; trimmed PV charge power
        self.pass_next = 0.0       # time of the next trim step
        self.pass_pv_pending = False  # initial DP 156 write still pending?
        self.pass_soc_warned = False  # warned that a meter target blocks it?
        self.pv_reopen = False     # PV limit still to reopen after pass-through?
        self.grid_fail = False     # meter failure already handled?
        self.backup_on = False     # manual backup charging requested?
        self.backup_power = BACKUP_POWER_DEFAULT
        self.surplus_on = False    # automatic surplus charging enabled?
        self.surplus_active = False   # charging from surplus right now?
        self.surplus_since = 0.0   # when surplus was first seen
        self.surplus_gone = 0.0    # when surplus last disappeared
        self.charge_setpoint = 0.0  # tracked DP 122 while charging
        self.charge_before = None  # DP 122 to put back afterwards
        self.mode_wanted = MODE_GRID
        self.neighbour_power = None   # neighbouring battery, W
        self.neighbour_ts = 0.0
        self.phases = [None, None, None]   # smoothed, W per phase
        self.phase_sum = None       # smoothed balanced total, W
        self.phase_ts = 0.0
        self.ep_phase = int(os.getenv("EP_PHASE", "1"))   # 1..3
        self.probe_at = 0.0         # when the running probe was started
        self.probe_meter = None     # smoothed sum before the step
        self.probe_draw = None      # DP 137 before the step
        self.phase_logged = 0.0     # when the phases were last written out
        self.setpoint = 0.0            # internal setpoint: >0 export, <0 charge
        self.events = []           # event list for the dashboard
        self.last_status = None    # previous device status (for events)
        self.last_dir = None       # last direction: on / charging / off
        self.was_offline = False   # disconnect already reported?
        self.shelly_ip = SHELLY_IP
        self.shelly_power = None
        self.shelly_on = None
        self.shelly_fails = 0
        self.shelly2_ip = SHELLY2_IP
        self.shelly2_power = None
        self.shelly2_on = None
        self.shelly2_fails = 0
        self.ac_storage1_ip = AC_STORAGE1_IP
        self.ac_storage1_power = None
        self.ac_storage1_on = None
        self.ac_storage1_ts = 0.0
        self.ac_storage1_fails = 0
        self.ac_storage2_ip = AC_STORAGE2_IP
        self.ac_storage2_power = None
        self.ac_storage2_on = None
        self.ac_storage2_ts = 0.0
        self.ac_storage2_fails = 0
        self.ac_storage_wait = False

    def tune_get(self, key):
        """Reads a controller parameter under the lock."""
        with self.lock:
            return self.tune[key]

    def tune_set(self, key, value):
        with self.lock:
            self.tune[key] = value

    def merge(self, dps):
        """Merges values into the cache and returns what changed.

        Implausible readings are dropped along the way: 16-bit overflows are
        converted back to negative numbers, and anything outside what is
        physically possible is discarded entirely.
        """
        changed = {}
        with self.lock:
            for k, v in dps.items():
                plausi = PLAUSIBLE_MAX.get(k)
                if plausi is not None and isinstance(v, int):
                    if v >= UINT16_SCHWELLE:
                        v = v - 65536
                    if abs(v) > plausi or (v < 0 and k not in NEGATIVE_OK):
                        log.warning("DP %s: %s W implausible - discarded", k, v)
                        continue
                if self.dps.get(k) != v:
                    changed[k] = (self.dps.get(k), v)
                self.dps[k] = v
        return changed

    def snapshot(self):
        with self.lock:
            return dict(self.dps)


st = State()


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def publish_discovery(client):
    """Creates the HA entities. retain=True so they survive restarts."""
    for dp, key, name, unit, dev_cls, state_cls, _factor in SENSORS:
        cfg = {
            "name": name,
            "unique_id": f"ep2500_{key}",
            "state_topic": f"{BASE}/state",
            "value_template": "{{ value_json." + key + " }}",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if dev_cls:
            cfg["device_class"] = dev_cls
        if state_cls:
            cfg["state_class"] = state_cls
        client.publish(f"{DISC}/sensor/ep2500/{key}/config",
                       json.dumps(cfg), retain=True)

    # Export limit as a number entity
    client.publish(f"{DISC}/number/ep2500/limit/config", json.dumps({
        "name": "Export limit",
        "unique_id": "ep2500_limit",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.limit }}",
        "command_topic": f"{BASE}/limit/set",
        "min": LIMIT_MIN, "max": LIMIT_MAX, "step": 5,
        "unit_of_measurement": "W",
        "mode": "box",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Control on/off
    client.publish(f"{DISC}/switch/ep2500/control/config", json.dumps({
        "name": "Zero-export control",
        "unique_id": "ep2500_control",
        "state_topic": f"{BASE}/control/state",
        "command_topic": f"{BASE}/control/set",
        "payload_on": "ON", "payload_off": "OFF",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Meter target: aim for this instead of 0
    client.publish(f"{DISC}/number/ep2500/correction/config", json.dumps({
        "name": "Meter target (+ import / - export)",
        "unique_id": "ep2500_correction",
        "state_topic": f"{BASE}/correction/state",
        "command_topic": f"{BASE}/correction/set",
        "min": METER_TARGET_MIN, "max": METER_TARGET_MAX, "step": 10,
        "unit_of_measurement": "W",
        "mode": "box",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # The meter reading the controller actually sees
    client.publish(f"{DISC}/sensor/ep2500/grid/config", json.dumps({
        "name": "Meter power (controller)",
        "unique_id": "ep2500_grid",
        "state_topic": f"{BASE}/grid",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Battery charge power limit (DP 122) - applies to PV AND grid charging.
    # At 0 the device stops taking in any solar power at all.
    client.publish(f"{DISC}/number/ep2500/charge/config", json.dumps({
        "name": "Battery charge limit",
        "unique_id": "ep2500_charge",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.charge }}",
        "command_topic": f"{BASE}/charge/set",
        "min": 0, "max": CHARGE_HW_MAX, "step": 50,
        "unit_of_measurement": "W",
        "mode": "box",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Backflow prevention (DP 118). Switched on it blocks export to the grid -
    # the device then goes to standby.
    client.publish(f"{DISC}/switch/ep2500/backflow/config", json.dumps({
        "name": "Backflow prevention",
        "icon": "mdi:transmission-tower-export",
        "unique_id": "ep2500_backflow",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.backflow else 'OFF' }}",
        "command_topic": f"{BASE}/backflow/set",
        "payload_on": "ON", "payload_off": "OFF",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # The controller's internal setpoint: >0 export, <0 charge
    client.publish(f"{DISC}/sensor/ep2500/setpoint/config", json.dumps({
        "name": "Controller setpoint",
        "unique_id": "ep2500_soll",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.setpoint }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Off-grid socket on the device (DP 119). It wakes the inverter from
    # standby, so it costs idle power even with nothing plugged in.
    client.publish(f"{DISC}/switch/ep2500/offgrid/config", json.dumps({
        "name": "Off-grid socket",
        "unique_id": "ep2500_offgrid",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.offgrid else 'OFF' }}",
        "command_topic": f"{BASE}/offgrid/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:power-socket-de",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Metering Shelly as on independent cross-check
    client.publish(f"{DISC}/sensor/ep2500/shelly/config", json.dumps({
        "name": "Shelly power",
        "unique_id": "ep2500_shelly",
        "state_topic": f"{BASE}/shelly",
        "value_template": "{{ value_json.power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": "mdi:power-plug",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Hard on/off switch. Disconnects the device from mains entirely - the
    # bridge loses its connection while it is off.
    client.publish(f"{DISC}/switch/ep2500/shelly/config", json.dumps({
        "name": "Shelly (mains disconnect)",
        "unique_id": "ep2500_shelly_switch",
        "state_topic": f"{BASE}/shelly",
        "value_template": "{{ value_json.state }}",
        "command_topic": f"{BASE}/shelly/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:power-plug",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Second Shelly, for example on a north-facing balcony PV system
    client.publish(f"{DISC}/sensor/ep2500/shelly2/config", json.dumps({
        "name": "North PV power",
        "unique_id": "ep2500_shelly2",
        "state_topic": f"{BASE}/shelly2",
        "value_template": "{{ value_json.power }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/switch/ep2500/shelly2/config", json.dumps({
        "name": "North PV",
        "unique_id": "ep2500_shelly2_switch",
        "state_topic": f"{BASE}/shelly2",
        "value_template": "{{ value_json.state }}",
        "command_topic": f"{BASE}/shelly2/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:solar-panel",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/text/ep2500/shelly2_ip/config", json.dumps({
        "name": "North PV Shelly IP",
        "unique_id": "ep2500_shelly2_ip",
        "state_topic": f"{BASE}/shelly2_ip",
        "command_topic": f"{BASE}/shelly2_ip/set",
        "entity_category": "config",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/text/ep2500/shelly_ip/config", json.dumps({
        "name": "Shelly IP",
        "unique_id": "ep2500_shelly_ip",
        "state_topic": f"{BASE}/shelly_ip",
        "command_topic": f"{BASE}/shelly_ip/set",
        "max": 15,
        "icon": "mdi:ip-network",
        "entity_category": "config",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Shelly plugs measuring AC-coupled storage charging power. Their relay
    # switches remain directly controllable from Home Assistant.
    for nr in (1, 2):
        key = f"ac_storage{nr}"
        label = f"AC storage {nr}"
        client.publish(f"{DISC}/sensor/ep2500/{key}/config", json.dumps({
            "name": f"{label} charging power",
            "unique_id": f"ep2500_{key}_power",
            "state_topic": f"{BASE}/{key}",
            "value_template": "{{ value_json.power }}",
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "icon": "mdi:battery-charging",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }), retain=True)
        client.publish(f"{DISC}/switch/ep2500/{key}/config", json.dumps({
            "name": label,
            "unique_id": f"ep2500_{key}_switch",
            "state_topic": f"{BASE}/{key}",
            "value_template": "{{ value_json.state }}",
            "command_topic": f"{BASE}/{key}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "icon": "mdi:battery-arrow-up",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }), retain=True)
        client.publish(f"{DISC}/text/ep2500/{key}_ip/config", json.dumps({
            "name": f"{label} Shelly IP",
            "unique_id": f"ep2500_{key}_ip",
            "state_topic": f"{BASE}/{key}_ip",
            "command_topic": f"{BASE}/{key}_ip/set",
            "max": 15,
            "icon": "mdi:ip-network",
            "entity_category": "config",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }), retain=True)

    for key, name in (
            ("ac_storage_charge_power", "AC storage charging power total"),
            ("meter_target_effective", "Effective meter target")):
        client.publish(f"{DISC}/sensor/ep2500/{key}/config", json.dumps({
            "name": name,
            "unique_id": f"ep2500_{key}",
            "state_topic": f"{BASE}/state",
            "value_template": "{{ value_json." + key + " }}",
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }), retain=True)

    # The device's own SoC limits
    for key, dp, name, icon in (
            ("socmax", DP_SOC_MAX, "Charge stop", "mdi:battery-charging-high"),
            ("socmin", DP_SOC_MIN, "Discharge stop", "mdi:battery-low")):
        client.publish(f"{DISC}/number/ep2500/{key}/config", json.dumps({
            "name": name,
            "unique_id": f"ep2500_{key}",
            "state_topic": f"{BASE}/state",
            "value_template": "{{ value_json." + key + " }}",
            "command_topic": f"{BASE}/{key}/set",
            "min": 0, "max": 100, "step": 1,
            "unit_of_measurement": "%",
            "device_class": "battery",
            "icon": icon,
            "mode": "box",
            "entity_category": "config",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }), retain=True)

    # Pass-through: with a full battery, send the PV surplus to the grid
    # instead of holding zero export
    client.publish(f"{DISC}/switch/ep2500/passthrough/config", json.dumps({
        "name": "Pass-through when battery full",
        "unique_id": "ep2500_passthrough",
        "state_topic": f"{BASE}/passthrough/state",
        "command_topic": f"{BASE}/passthrough/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:transmission-tower-export",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/binary_sensor/ep2500/pass_active/config", json.dumps({
        "name": "Pass-through running",
        "unique_id": "ep2500_pass_active",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.passthrough else 'OFF' }}",
        "payload_on": "ON", "payload_off": "OFF",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/sensor/ep2500/runtime/config", json.dumps({
        "name": "Remaining runtime",
        "unique_id": "ep2500_runtime",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.runtime }}",
        "unit_of_measurement": "h",
        "device_class": "duration",
        "icon": "mdi:battery-clock",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # System fault (DP 149). The value latches after a fault and only clears
    # when the device is restarted - without a sensor the system runs for days
    # carrying a fault nobody sees.
    client.publish(f"{DISC}/sensor/ep2500/fault/config", json.dumps({
        "name": "System fault",
        "unique_id": "ep2500_fault",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.fault_text }}",
        "json_attributes_topic": f"{BASE}/state",
        "json_attributes_template": "{{ {'raw': value_json.fault} | tojson }}",
        "icon": "mdi:alert-circle-outline",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/binary_sensor/ep2500/fault_active/config", json.dumps({
        "name": "Fault",
        "unique_id": "ep2500_fault_active",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.fault_active else 'OFF' }}",
        "payload_on": "ON", "payload_off": "OFF",
        "device_class": "problem",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Per-phase readings from the meter. The zero-export controller does not
    # use these - it keeps its own averaged value - but the surplus logic does,
    # and seeing them makes it obvious which phase is doing what.
    for nr in (1, 2, 3):
        client.publish(f"{DISC}/sensor/ep2500/phase{nr}/config", json.dumps({
            "name": f"Phase {nr}",
            "unique_id": f"ep2500_phase{nr}",
            "state_topic": f"{BASE}/state",
            "value_template": "{{ value_json.phase" + str(nr) + " }}",
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }), retain=True)

    client.publish(f"{DISC}/select/ep2500/phase/config", json.dumps({
        "name": "EP2500 phase",
        "unique_id": "ep2500_ep_phase",
        "state_topic": f"{BASE}/phase",
        "command_topic": f"{BASE}/phase/set",
        "options": ["1", "2", "3"],
        "icon": "mdi:sine-wave",
        "entity_category": "config",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/sensor/ep2500/own_phase_load/config", json.dumps({
        "name": "Other load on own phase",
        "unique_id": "ep2500_own_phase_load",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.own_phase_load }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Backup charging: manual, overrides everything else
    client.publish(f"{DISC}/switch/ep2500/backup/config", json.dumps({
        "name": "Backup charge",
        "unique_id": "ep2500_backup",
        "state_topic": f"{BASE}/backup/state",
        "command_topic": f"{BASE}/backup/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:home-lightning-bolt",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/number/ep2500/backup_power/config", json.dumps({
        "name": "Backup charge power",
        "unique_id": "ep2500_backup_power",
        "state_topic": f"{BASE}/backup/power",
        "command_topic": f"{BASE}/backup/power/set",
        "min": 0, "max": CHARGE_HW_MAX, "step": 50,
        "unit_of_measurement": "W",
        "device_class": "power",
        "mode": "box",
        "icon": "mdi:battery-charging-high",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # AC surplus charging: automatic
    client.publish(f"{DISC}/switch/ep2500/surplus/config", json.dumps({
        "name": "AC surplus charging",
        "unique_id": "ep2500_surplus",
        "state_topic": f"{BASE}/surplus/state",
        "command_topic": f"{BASE}/surplus/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:solar-power-variant",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/binary_sensor/ep2500/surplus_active/config", json.dumps({
        "name": "Taking surplus",
        "unique_id": "ep2500_surplus_active",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.surplus_active else 'OFF' }}",
        "payload_on": "ON", "payload_off": "OFF",
        "device_class": "running",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Recording: switch and status display
    client.publish(f"{DISC}/switch/ep2500/record/config", json.dumps({
        "name": "Record log",
        "unique_id": "ep2500_record",
        "state_topic": f"{BASE}/record/state",
        "command_topic": f"{BASE}/record/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:record-rec",
        "entity_category": "config",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/sensor/ep2500/record_info/config", json.dumps({
        "name": "Recording",
        "unique_id": "ep2500_record_info",
        "state_topic": f"{BASE}/record/info",
        "value_template": "{{ value_json.text }}",
        "json_attributes_topic": f"{BASE}/record/info",
        "icon": "mdi:file-document-outline",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Event log of the last 48 hours
    client.publish(f"{DISC}/sensor/ep2500/events/config", json.dumps({
        "name": "Events",
        "unique_id": "ep2500_events",
        "state_topic": f"{BASE}/events",
        "value_template": "{{ value_json.last[:250] }}",
        "json_attributes_topic": f"{BASE}/events",
        "json_attributes_template": "{{ {'entries': value_json.entries, "
                                    "'count': value_json.count} | tojson }}",
        "icon": "mdi:format-list-bulleted",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Controller parameters as number entities
    for key, (name, vmin, vmax, step, unit, _typ) in TUNABLES.items():
        cfg = {
            "name": name,
            "unique_id": f"ep2500_tune_{key}",
            "state_topic": f"{BASE}/tune/{key}",
            "command_topic": f"{BASE}/tune/{key}/set",
            "min": vmin, "max": vmax, "step": step,
            "mode": "box",
            "entity_category": "config",
            "availability_topic": f"{BASE}/available",
            "device": DEVICE_INFO,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        client.publish(f"{DISC}/number/ep2500/tune_{key}/config",
                       json.dumps(cfg), retain=True)

    # Remove retired entities (on empty payload deletes them in HA)
    for path_ in ("switch/ep2500/gridcharge", "number/ep2500/tune_charge_max",
                 "number/ep2500/tune_hyst", "sensor/ep2500/pv_strings",
                 "number/ep2500/tune_pass_marge",
                 "binary_sensor/ep2500/offgrid_live"):
        client.publish(f"{DISC}/{path_}/config", "", retain=True)

    log.info("Discovery published")


def on_connect(client, userdata, flags, rc, properties=None):
    log.info("MQTT connected (rc=%s)", rc)
    publish_discovery(client)
    for t in (f"{BASE}/limit/set", f"{BASE}/control/set", f"{BASE}/charge/set",
              f"{BASE}/backflow/set", f"{BASE}/correction/set",
              f"{BASE}/passthrough/set",
              f"{BASE}/socmax/set", f"{BASE}/socmin/set",
              f"{BASE}/shelly_ip/set", f"{BASE}/shelly/set",
              f"{BASE}/shelly2_ip/set", f"{BASE}/shelly2/set",
              f"{BASE}/phase/set", f"{BASE}/backup/set",
              f"{BASE}/backup/power/set", f"{BASE}/surplus/set",
              f"{BASE}/ac_storage1_ip/set", f"{BASE}/ac_storage1/set",
              f"{BASE}/ac_storage2_ip/set", f"{BASE}/ac_storage2/set",
              f"{BASE}/offgrid/set", f"{BASE}/record/set"):
        client.subscribe(t)
    if GRID_SOURCE == "mqtt":
        client.subscribe(GRID_TOPIC)
    client.subscribe(f"{BASE}/tune/+/set")

    # Read retained states back so settings are restored after a restart
    # instead of being overwritten with the defaults.
    client.subscribe(f"{BASE}/tune/+")
    client.subscribe(f"{BASE}/correction/state")
    client.subscribe(f"{BASE}/control/state")
    client.subscribe(f"{BASE}/events")
    client.subscribe(f"{BASE}/passthrough/state")
    client.subscribe(f"{BASE}/shelly_ip")
    client.subscribe(f"{BASE}/shelly2_ip")
    client.subscribe(f"{BASE}/phase")
    client.subscribe(f"{BASE}/backup/power")
    client.subscribe(f"{BASE}/surplus/state")
    if NEIGHBOUR_TOPIC:
        client.subscribe(NEIGHBOUR_TOPIC)
    client.subscribe(f"{BASE}/ac_storage1_ip")
    client.subscribe(f"{BASE}/ac_storage2_ip")

    # Re-send the availability state after on MQTT reconnect, otherwise HA
    # shows the entities as permanently unavailable.
    client.publish(f"{BASE}/available",
                   "online" if st.online else "offline", retain=True)

    threading.Timer(3.0, publish_settings).start()


def publish_settings():
    """Publishes everything that did not come back from retained MQTT."""
    if "control" not in st.restored:
        mqttc.publish(f"{BASE}/control/state",
                      "ON" if st.control_on else "OFF", retain=True)
    # Do not publish empty addresses. An empty retained message deletes the
    # stored value in the broker, and the text field in Home Assistant then
    # shows "empty value" even though something was in it before.
    if "shelly_ip" not in st.restored and st.shelly_ip:
        mqttc.publish(f"{BASE}/shelly_ip", st.shelly_ip, retain=True)
    if "phase" not in st.restored:
        mqttc.publish(f"{BASE}/phase", str(st.ep_phase), retain=True)
    if "backup_power" not in st.restored:
        mqttc.publish(f"{BASE}/backup/power", str(st.backup_power), retain=True)
    if "surplus" not in st.restored:
        mqttc.publish(f"{BASE}/surplus/state",
                      "ON" if st.surplus_on else "OFF", retain=True)
    mqttc.publish(f"{BASE}/backup/state",
                  "ON" if st.backup_on else "OFF", retain=True)
    if "shelly2_ip" not in st.restored and st.shelly2_ip:
        mqttc.publish(f"{BASE}/shelly2_ip", st.shelly2_ip, retain=True)
    for key in ("ac_storage1", "ac_storage2"):
        ip = getattr(st, f"{key}_ip")
        if f"{key}_ip" not in st.restored and ip:
            mqttc.publish(f"{BASE}/{key}_ip", ip, retain=True)
    if "passthrough" not in st.restored:
        mqttc.publish(f"{BASE}/passthrough/state",
                      "ON" if st.pass_on else "OFF", retain=True)
    if "correction" not in st.restored:
        mqttc.publish(f"{BASE}/correction/state", st.correction, retain=True)
    for key, val in st.tune.items():
        if key not in st.restored:
            mqttc.publish(f"{BASE}/tune/{key}", val, retain=True)
    log.info("Controller parameters: %s", st.tune)
    # When reading logs it helps to see which addresses actually arrived - on
    # environment variable that never made it is otherwise easy to miss.
    log.info("Shelly addresses: mains disconnect=%r  north PV=%r  "
             "AC storage 1=%r  AC storage 2=%r",
             st.shelly_ip or "(empty)", st.shelly2_ip or "(empty)",
             st.ac_storage1_ip or "(empty)",
             st.ac_storage2_ip or "(empty)")


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="replace").strip()
    topic = msg.topic

    # Set a controller parameter: ep2500/tune/<key>/set
    if topic.startswith(f"{BASE}/tune/") and topic.endswith("/set"):
        key = topic.split("/")[-2]
        apply_tune(key, payload, source="HA")
        return

    # Take a stored value after a restart: ep2500/tune/<key>
    if topic.startswith(f"{BASE}/tune/"):
        key = topic.split("/")[-1]
        if key in TUNABLES and key not in st.restored:
            if apply_tune(key, payload, source="restored", echo=False):
                st.restored.add(key)
        return

    if topic == f"{BASE}/control/state":
        if "control" not in st.restored:
            st.control_on = payload.upper() == "ON"
            st.restored.add("control")
            log.info("Control %s (restored)",
                     "on" if st.control_on else "off")
        return

    if topic == f"{BASE}/events":
        if "events" not in st.restored:
            st.restored.add("events")
            try:
                old = json.loads(payload).get("entries", [])
                now_ts = time.time()
                with st.lock:
                    st.events = [e for e in old
                                 if now_ts - e.get("ts", 0) <= EVENT_MAX_AGE][-EVENT_MAX:]
                log.info("%d earlier events restored", len(st.events))
            except Exception:
                pass
        return

    if topic == f"{BASE}/shelly_ip":
        if "shelly_ip" not in st.restored and payload:
            st.shelly_ip = payload
            st.restored.add("shelly_ip")
            log.info("Shelly address %s (restored)", payload)
        return

    for nr in (1, 2):
        key = f"ac_storage{nr}"
        if topic == f"{BASE}/{key}/set":
            shelly_switch(payload.upper() == "ON", nr=nr + 2)
            return
        if topic == f"{BASE}/{key}_ip/set":
            ip = payload.strip()
            setattr(st, f"{key}_ip", ip)
            setattr(st, f"{key}_fails", 0)
            setattr(st, f"{key}_power", None)
            setattr(st, f"{key}_ts", 0.0)
            st.restored.add(f"{key}_ip")
            client.publish(f"{BASE}/{key}_ip", ip, retain=True)
            event(f"AC storage {nr} Shelly address changed to {ip or '(empty)'}")
            return
        if topic == f"{BASE}/{key}_ip":
            if f"{key}_ip" not in st.restored and payload:
                setattr(st, f"{key}_ip", payload)
                st.restored.add(f"{key}_ip")
                log.info("AC storage %s Shelly address %s (restored)",
                         nr, payload)
            return

    if topic == f"{BASE}/shelly2/set":
        shelly_switch(payload.upper() == "ON", nr=2)
        return

    if topic == f"{BASE}/shelly2_ip/set":
        st.shelly2_ip = payload.strip()
        st.shelly2_fails = 0
        st.restored.add("shelly2_ip")
        # Publish retained like the first address, otherwise the entry is
        # gone again after the next restart.
        client.publish(f"{BASE}/shelly2_ip", st.shelly2_ip, retain=True)
        event(f"North PV Shelly address changed to {st.shelly2_ip}")
        return

    if topic == f"{BASE}/shelly2_ip":
        if "shelly2_ip" not in st.restored and payload:
            st.shelly2_ip = payload
            st.restored.add("shelly2_ip")
            log.info("North PV Shelly address %s (restored)", payload)
        return

    if topic == f"{BASE}/shelly/set":
        shelly_switch(payload.upper() == "ON")
        return

    if topic == f"{BASE}/shelly_ip/set":
        st.shelly_ip = payload
        st.shelly_fails = 0
        st.restored.add("shelly_ip")
        client.publish(f"{BASE}/shelly_ip", payload, retain=True)
        event(f"Shelly address changed to {payload}")
        return

    for key, dp, name in (("socmax", DP_SOC_MAX, "Charge stop"),
                          ("socmin", DP_SOC_MIN, "Discharge stop")):
        if topic == f"{BASE}/{key}/set":
            val = parse_number(payload)
            if val is not None:
                val = max(0, min(100, int(val)))
                res = tuya_call("set_value", int(dp), val)
                ok, reason_txt = response_ok(res)
                if ok:
                    log_changes(st.merge({dp: val}), "(HA)")
                    event(f"{name} set to {val} %")
                else:
                    log.warning("%s rejected: %s", name, reason_txt)
                publish_state()
            return

    if topic == f"{BASE}/passthrough/state":
        if "passthrough" not in st.restored:
            st.pass_on = payload.upper() == "ON"
            st.restored.add("passthrough")
            log.info("Pass-through %s (restored)",
                     "enabled" if st.pass_on else "disabled")
        return

    if topic == f"{BASE}/passthrough/set":
        st.pass_on = payload.upper() == "ON"
        st.restored.add("passthrough")
        client.publish(f"{BASE}/passthrough/state",
                       "ON" if st.pass_on else "OFF", retain=True)
        event("Pass-through " + ("enabled" if st.pass_on else "disabled"))
        if not st.pass_on and (st.pass_active or st.pass_pv_pending
                               or st.pv_reopen):
            stop_passthrough("disabled in Home Assistant",
                             safe_limit=LIMIT_SAFE)
        return

    if topic == f"{BASE}/backflow/set":
        block = payload.upper() == "ON"
        if set_backflow(block, source="HA"):
            event("Backflow prevention "
                  + ("on - export blocked" if block
                     else "off - export possible"))
            threading.Timer(VERIFY_DELAY, verify_switch,
                            (DP_BACKFLOW, block,
                             "Backflow prevention")).start()
        else:
            event("Switching backflow prevention failed")
        publish_state()
        return

    if NEIGHBOUR_TOPIC and topic == NEIGHBOUR_TOPIC:
        value = parse_number(payload, NEIGHBOUR_FIELD)
        if value is not None:
            with st.lock:
                st.neighbour_power = float(value)
                st.neighbour_ts = time.time()
        return

    if topic == f"{BASE}/phase/set":
        try:
            nr = int(payload)
        except ValueError:
            return
        if nr not in (1, 2, 3):
            return
        with st.lock:
            st.ep_phase = nr
        st.restored.add("phase")
        client.publish(f"{BASE}/phase", str(nr), retain=True)
        event(f"EP2500 is on phase {nr}")
        return

    if topic == f"{BASE}/phase":
        if "phase" not in st.restored and payload in ("1", "2", "3"):
            with st.lock:
                st.ep_phase = int(payload)
            st.restored.add("phase")
        return

    if topic == f"{BASE}/backup/power/set":
        try:
            watt = max(0, min(CHARGE_HW_MAX, int(float(payload))))
        except ValueError:
            return
        st.backup_power = watt
        st.restored.add("backup_power")
        client.publish(f"{BASE}/backup/power", str(watt), retain=True)
        if st.backup_on:
            set_charge(watt, reason="backup charge")
        publish_state()
        return

    if topic == f"{BASE}/backup/power":
        if "backup_power" not in st.restored and payload:
            try:
                st.backup_power = int(float(payload))
                st.restored.add("backup_power")
            except ValueError:
                pass
        return

    if topic == f"{BASE}/backup/set":
        want = payload.upper() == "ON"
        if want and not st.backup_on:
            start_backup()
        elif not want and st.backup_on:
            st.backup_on = False
            stop_charging("backup switched off")
        client.publish(f"{BASE}/backup/state", "ON" if st.backup_on else "OFF",
                       retain=True)
        publish_state()
        return

    if topic == f"{BASE}/surplus/set":
        st.surplus_on = payload.upper() == "ON"
        st.restored.add("surplus")
        if not st.surplus_on and st.surplus_active:
            stop_charging("surplus charging switched off")
        event("AC surplus charging "
              + ("enabled" if st.surplus_on else "disabled"))
        client.publish(f"{BASE}/surplus/state",
                       "ON" if st.surplus_on else "OFF", retain=True)
        publish_state()
        return

    if topic == f"{BASE}/surplus/state":
        if "surplus" not in st.restored and payload:
            st.surplus_on = payload.upper() == "ON"
            st.restored.add("surplus")
        return

    if topic == f"{BASE}/record/set":
        on = payload.upper() == "ON"
        if on:
            # Any exception here would be swallowed by the MQTT callback and
            # the switch would simply do nothing - which is exactly how the
            # renamed start()/stop() methods went unnoticed.
            try:
                name = recorder.start()
            except Exception as exc:
                log.exception("Starting the recording failed")
                event(f"Starting the recording failed: {exc}")
                name = None
            if name:
                event(f"Recording started: {name}")
            else:
                # Starting failed - do not leave the switch on, or the
                # dashboard shows a recording that does not exist.
                event("Recording could not be started")
                on = False
        else:
            try:
                name = recorder.stop()
            except Exception as exc:
                log.exception("Stopping the recording failed")
                event(f"Stopping the recording failed: {exc}")
                name = None
            if name:
                event(f"Recording stopped: {name}")
        client.publish(f"{BASE}/record/state", "ON" if on else "OFF", retain=True)
        publish_record_info()
        return

    if topic == f"{BASE}/offgrid/set":
        on = payload.upper() == "ON"
        res = tuya_call("set_value", int(DP_OFFGRID), on)
        ok, reason_txt = response_ok(res)
        if ok:
            st.merge({DP_OFFGRID: on})
        if ok:
            event("Off-grid socket "
                  + ("switched on" if on else "switched off"))
            locked, soc, threshold = offgrid_locked(st.snapshot())
            if on and locked:
                # The device accepts the command but does not carry it out.
                # Without this note it looks like a bug in the bridge.
                event(f"Note: state of charge {soc} %, the output only returns at "
                      f"{threshold} % (discharge stop +{OFFGRID_SOC_MARGIN})")
            # The cache now holds the requested value. Whether the device
            # really took it only shows on a read-back.
            threading.Timer(VERIFY_DELAY, verify_switch,
                            (DP_OFFGRID, on, "Off-grid socket")).start()
        else:
            # This event used to be raised on a rejection too - the log then
            # claimed a switching action that never happened.
            log.warning("Off-grid socket rejected: %s", reason_txt)
            event(f"Switching off-grid socket failed: {reason_txt}")
        publish_state()
        return

    if topic == f"{BASE}/charge/set":
        val = parse_number(payload)
        if val is not None:
            # Move the stored setpoint along as well, otherwise the guard
            # resets the limit within 30 s.
            val = max(0, min(CHARGE_HW_MAX, int(val)))
            st.tune_set("charge_limit", val)
            mqttc.publish(f"{BASE}/tune/charge_limit", val, retain=True)
            set_charge(val, reason="manual")
        return

    if topic == f"{BASE}/correction/state":
        if "correction" not in st.restored:
            val = parse_number(payload)
            if val is not None:
                st.correction = clamp_meter_target(val)
                st.restored.add("correction")
                if st.correction != int(round(val)):
                    client.publish(f"{BASE}/correction/state", st.correction,
                                   retain=True)
        return

    if topic == GRID_TOPIC:
        if GRID_SOURCE != "mqtt":
            return          # HTTP source active, MQTT must not override it
        val = parse_number(payload, GRID_JSON_KEY)
        if val is None:
            return
        # Same checks and jump detection as the HTTP path. The function also
        # publishes the value itself, otherwise the "Meter power (controller)"
        # sensor in HA would stay empty.
        accept_meter_value(val, "MQTT")
        return

    if topic == f"{BASE}/limit/set":
        val = parse_number(payload)
        if val is not None:
            set_limit(int(val), reason="manual")
        return

    if topic == f"{BASE}/control/set":
        st.control_on = payload.upper() == "ON"
        st.restored.add("control")
        client.publish(f"{BASE}/control/state",
                       "ON" if st.control_on else "OFF", retain=True)
        event("Control " + ("switched on" if st.control_on else "switched off"))
        if not st.control_on:
            stop_passthrough("control switched off", safe_limit=LIMIT_SAFE)
        return

    if topic == f"{BASE}/correction/set":
        val = parse_number(payload)
        if val is not None:
            st.correction = clamp_meter_target(val)
            st.restored.add("correction")
            client.publish(f"{BASE}/correction/state", st.correction, retain=True)
            log.info("Meter target = %s W", st.correction)
            event(f"Meter target set to {st.correction:+d} W")


def apply_tune(key, payload, source="", echo=True):
    """Takes a controller parameter, clamped to its allowed range."""
    if key not in TUNABLES:
        return False
    name, vmin, vmax, _step, unit, typ = TUNABLES[key]
    val = parse_number(payload)
    if val is None:
        return False
    val = typ(max(vmin, min(vmax, val)))
    st.tune_set(key, val)
    if echo:
        mqttc.publish(f"{BASE}/tune/{key}", val, retain=True)
    log.info("%s = %s%s (%s)", name, val, f" {unit}" if unit else "", source)
    return True


def parse_number(payload, json_key=None):
    """Accepts '812', '812.0' or {'power': 812}.

    NaN and infinity are rejected - a later int() would otherwise raise and
    abort the control step.
    """
    try:
        value = float(payload)
        return value if math.isfinite(value) else None
    except ValueError:
        pass
    try:
        data = json.loads(payload)
        if json_key and isinstance(data, dict) and json_key in data:
            value = float(data[json_key])
            return value if math.isfinite(value) else None
    except Exception:
        pass
    log.warning("Unreadable payload: %r", payload[:80])
    return None


def clamp_meter_target(value):
    """Clamps and rounds the desired meter power to its configured range."""
    return max(METER_TARGET_MIN,
               min(METER_TARGET_MAX, int(round(float(value)))))


def calculate_setpoint(grid, target, actual, current, gain, max_step):
    """Returns the next DP 121 setpoint for a signed meter target."""
    error = grid - target  # >0: output must rise; <0: output must fall
    current = min(actual + WINDUP_MARGIN, current)
    current += max(-max_step, min(max_step, error * gain))
    return max(0.0, min(float(LIMIT_MAX), current))


def compensated_meter_target(requested, storage_power, ready=True,
                             compensation_enabled=True):
    """Offsets a negative meter target by AC-storage charging power.

    Example: requested -300 W and 100 W measured charging gives an effective
    meter target of -200 W. At 300 W charging the effective target is 0 W.
    The contribution is capped, so it can never turn the request into import
    or create a positive feedback ramp.
    """
    requested = float(requested)
    if requested >= 0 or not compensation_enabled:
        return requested
    if not ready:
        return 0.0
    absorbed = min(-requested, max(0.0, float(storage_power)))
    return min(0.0, requested + absorbed)


def ac_storage_charge_power(now=None):
    """Returns (watts, ready, configured) for enabled AC-storage Shellys."""
    now = time.time() if now is None else now
    total = 0.0
    configured = False
    active = False
    with st.lock:
        values = [
            (st.ac_storage1_ip, st.ac_storage1_power,
             st.ac_storage1_on, st.ac_storage1_ts),
            (st.ac_storage2_ip, st.ac_storage2_power,
             st.ac_storage2_on, st.ac_storage2_ts),
        ]

    for ip, power, on, timestamp in values:
        if not ip:
            continue
        configured = True
        if on is False:
            continue
        active = True
        if (not isinstance(power, (int, float))
                or now - timestamp > AC_STORAGE_MAX_AGE):
            return 0.0, False, True
        if power > AC_STORAGE_NOISE:
            total += power

    # If Shellys are configured but all their relays are off, do not create
    # deliberate grid export that no storage unit can absorb.
    return total, (not configured or active), configured


mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ep2500-bridge")
if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.will_set(f"{BASE}/available", "offline", retain=True)
mqttc.on_connect = on_connect
mqttc.on_message = on_message


# --------------------------------------------------------------------------
# Tuya
# --------------------------------------------------------------------------

_tuya_requests = queue.Queue()


class TuyaRequest:
    """One command handed to the single thread that owns the Tuya socket."""

    def __init__(self, method, args, timeout):
        self.method = method
        self.args = args
        self.deadline = time.monotonic() + timeout
        self.done = threading.Event()
        self.result = None


def connect_device():
    d = tinytuya.OutletDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY, port=DEVICE_PORT)
    d.set_version(3.5)
    d.set_socketPersistent(True)
    d.set_socketRetryLimit(1)
    d.set_socketTimeout(TUYA_COMMAND_TIMEOUT)
    return d


def tuya_call(method, *args, timeout=TUYA_COMMAND_TIMEOUT):
    """Runs a command on the Tuya I/O thread and waits for its reply.

    TinyTuya uses one request/response socket and does not serialize access
    itself. Sending from MQTT, controller and verification threads in parallel
    can make one caller consume another caller's reply. All socket operations
    therefore go through this queue.
    """
    if not st.online:
        return {"Error": "device offline", "Err": "offline"}
    request = TuyaRequest(method, args, timeout)
    _tuya_requests.put(request)
    if not request.done.wait(timeout):
        return {"Error": "Tuya command timed out", "Err": "timeout"}
    return request.result


def process_tuya_requests(d, maximum=32):
    """Executes queued commands; called only by ``tuya_loop``."""
    for _ in range(maximum):
        try:
            request = _tuya_requests.get_nowait()
        except queue.Empty:
            break

        if time.monotonic() >= request.deadline:
            request.result = {"Error": "Tuya command expired", "Err": "timeout"}
            request.done.set()
            continue

        try:
            d.set_socketTimeout(TUYA_COMMAND_TIMEOUT)
            request.result = getattr(d, request.method)(*request.args)
        except Exception as exc:
            request.result = {"Error": str(exc), "Err": "exception"}
        finally:
            request.done.set()


def response_ok(res):
    """Checks TinyTuya's reply to a write.

    TinyTuya does not raise on errors; it returns a dict with on "Error" key.
    Without this check a failed command would count as successful and the
    requested value would land in the cache - Home Assistant would then show
    something that never reached the device.
    """
    if res is None:
        return False, "no response"
    if isinstance(res, dict) and "Error" in res:
        return False, f"{res.get('Error')} (Err {res.get('Err')})"
    return True, ""


def write_dp(dp, watt, vmax, reason=""):
    """Writes a power datapoint, hard-clamped to 0..vmax."""
    watt = max(0, min(vmax, int(watt)))
    name = {DP_LIMIT: "Export limit", DP_CHARGE: "Battery charge limit",
            DP_PV_LIMIT: "PV limit"}.get(dp, f"DP {dp}")
    res = tuya_call("set_value", int(dp), watt)

    ok, reason_txt = response_ok(res)
    if not ok:
        log.warning("%s -> %s W rejected: %s", name, watt, reason_txt)
        return False

    st.last_write = time.time()
    st.merge({dp: watt})
    log.info("%s -> %s W (%s)", name, watt, reason)
    return True


def set_limit(watt, reason=""):
    return write_dp(DP_LIMIT, watt, LIMIT_MAX, reason)


def set_charge(watt, reason=""):
    return write_dp(DP_CHARGE, watt, CHARGE_HW_MAX, reason)


def publish_record_info():
    """Publishes the recording state for the dashboard."""
    aborted = recorder.take_abort_reason()
    if aborted:
        event(f"Recording stopped on its own: {aborted}")
    i = recorder.info()
    if i["active"]:
        text = (f"running for {i['minutes']} min - {i['lines']} lines, "
                f"{i['kib']} KiB")
    elif i["file"]:
        text = f"stopped: {i['file']}"
    else:
        text = "no recording"
    i["text"] = text
    # Without REC_HOST there is no address to offer. Publishing
    # "http://:8099/" would put a dead link on the dashboard.
    i["url"] = f"http://{REC_HOST}:{REC_PORT}/" if REC_HOST else ""
    i["port"] = REC_PORT
    mqttc.publish(f"{BASE}/record/state",
                  "ON" if i["active"] else "OFF", retain=True)
    mqttc.publish(f"{BASE}/record/info", json.dumps(i), retain=True)


def publish_state():
    dps = st.snapshot()
    if not dps:
        return
    out = {}
    for dp, key, _n, _u, _dc, _sc, factor in SENSORS:
        v = dps.get(dp)
        if v is None:
            continue
        out[key] = round(v * factor, 2) if factor and isinstance(v, (int, float)) else v
    if DP_LIMIT in dps:
        out["limit"] = dps[DP_LIMIT]
    if DP_CHARGE in dps:
        out["charge"] = dps[DP_CHARGE]
    out["setpoint"] = int(round(st.setpoint))
    out["passthrough"] = st.pass_active
    storage_power, storage_ready, storage_configured = ac_storage_charge_power()
    out["ac_storage_charge_power"] = round(storage_power)
    out["meter_target"] = st.correction
    out["meter_target_effective"] = round(compensated_meter_target(
        st.correction, storage_power, storage_ready, storage_configured))
    error = dps.get("149")
    fault_txt, fault_serious = fault_text(error)
    out["fault"] = error if isinstance(error, int) else 0
    out["fault_text"] = fault_txt
    # A separate boolean: the raw value 2 is non-zero but harmless. A binary
    # sensor on the raw value would raise on alarm while the text next to it
    # reads "no fault".
    out["fault_active"] = fault_serious
    with st.lock:
        for nr in (1, 2, 3):
            value = st.phases[nr - 1]
            out[f"phase{nr}"] = round(value) if value is not None else None
        out["ep_phase"] = st.ep_phase
    other = own_phase_load(dps)
    out["own_phase_load"] = round(other) if other is not None else None
    out["backup"] = st.backup_on
    out["surplus_enabled"] = st.surplus_on
    out["surplus_active"] = st.surplus_active
    out["charge_setpoint"] = int(st.charge_setpoint)
    if DP_OFFGRID in dps:
        out["offgrid"] = bool(dps[DP_OFFGRID])
    if DP_BACKFLOW in dps:
        out["backflow"] = bool(dps[DP_BACKFLOW])
    if DP_SOC_MAX in dps:
        out["socmax"] = dps[DP_SOC_MAX]
    if DP_SOC_MIN in dps:
        out["socmin"] = dps[DP_SOC_MIN]

    # Remaining runtime: usable energy above the discharge-stop SoC divided by
    # the current discharge power. Only meaningful while discharging - it stays
    # empty when charging or in standby.
    soc = dps.get("102")
    batt = dps.get(DP_BATT)
    soc_min = dps.get("124", 0)
    if (isinstance(soc, (int, float)) and isinstance(batt, (int, float))
            and batt < -5):
        nutzbar = max(0.0, (soc - (soc_min or 0)) / 100.0 * BATT_WH)
        stunden = nutzbar / abs(batt)
        out["runtime"] = round(stunden, 1)
    else:
        out["runtime"] = None

    mqttc.publish(f"{BASE}/state", json.dumps(out))


def tuya_loop():
    """Holds the connection, merges delta frames, polls a full status."""
    while True:
        try:
            d = connect_device()
            data = d.status()
            if not (isinstance(data, dict) and "dps" in data):
                raise RuntimeError(f"Status query failed: {data}")
            # After a reconnect, comparing against the cache shows what
            # changed meanwhile - through the Oukitel app, for instance.
            log_changes(st.merge(data["dps"]), "(full status)")
            st.online = True
            mqttc.publish(f"{BASE}/available", "online", retain=True)
            publish_state()
            log.info("Connected, %d datapoints", len(data["dps"]))
            if st.was_offline:
                event("Connection to the device restored")
                st.was_offline = False

            next_hb = time.time() + HEARTBEAT
            next_full = time.time() + POLL_FULL

            while True:
                process_tuya_requests(d)
                # Keep receive() short so a command from MQTT or the control
                # loop does not sit behind an eight-second blocking read.
                d.set_socketTimeout(TUYA_RECEIVE_TIMEOUT)
                frame = d.receive()
                if isinstance(frame, dict):
                    if "dps" in frame:
                        log_changes(st.merge(frame["dps"]))
                        publish_state()
                    elif "Error" in frame:
                        raise RuntimeError(frame["Error"])

                now = time.time()
                if now >= next_hb:
                    d.heartbeat(nowait=True)
                    next_hb = now + HEARTBEAT
                if now >= next_full:
                    d.set_socketTimeout(TUYA_COMMAND_TIMEOUT)
                    full = d.status()
                    if isinstance(full, dict) and "dps" in full:
                        log_changes(st.merge(full["dps"]), "(poll)")
                        publish_state()
                    elif isinstance(full, dict) and "Error" in full:
                        raise RuntimeError(full["Error"])
                    next_full = now + POLL_FULL

        except Exception as exc:
            st.online = False
            mqttc.publish(f"{BASE}/available", "offline", retain=True)
            if not st.was_offline:
                event(f"Connection to the device lost ({exc})", also_log=False)
                st.was_offline = True
            log.warning("Connection lost (%s), retrying in 15 s", exc)
            time.sleep(15)


# --------------------------------------------------------------------------
# Eco Tracker (everHome Local API)
# --------------------------------------------------------------------------

def accept_phases(data):
    """Takes the per-phase readings and the balanced sum, smoothed.

    All four use the instantaneous fields and the same filter, so they stay
    comparable. Anything implausible is dropped rather than smoothed in.
    """
    values = []
    for field in ("power",) + PHASE_FIELDS:
        raw = data.get(field)
        if not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return
        if abs(raw) > ECO_ABSURD:
            return
        values.append(float(raw))

    with st.lock:
        previous = [st.phase_sum] + list(st.phases)
        smoothed = []
        for old, new in zip(previous, values):
            if old is None:
                smoothed.append(new)
            else:
                smoothed.append(old + (new - old) * PHASE_SMOOTH)
        st.phase_sum, st.phases = smoothed[0], smoothed[1:]
        st.phase_ts = time.time()


def log_phases():
    """Writes the phase readings to the log at a calm cadence."""
    if not PHASE_LOG_INTERVAL:
        return
    now = time.time()
    with st.lock:
        if now - st.phase_logged < PHASE_LOG_INTERVAL:
            return
        st.phase_logged = now
        values, total, phase = list(st.phases), st.phase_sum, st.ep_phase
    if total is None or any(v is None for v in values):
        return
    other = own_phase_load(st.snapshot())
    log.info("Phases | L1 %+5.0f | L2 %+5.0f | L3 %+5.0f | sum %+5.0f W | "
             "own L%d, others %s",
             values[0], values[1], values[2], total, phase,
             "?" if other is None else f"{other:+.0f} W")


def own_phase_load(dps):
    """Load on the EP2500's phase with its own contribution removed.

    The device knows what it is drawing or feeding (DP 137, positive while
    importing). Subtracting that leaves what everything else on that phase is
    doing - which is what point 1 and 2 of the phase idea were about.
    """
    with st.lock:
        phase = st.ep_phase
        value = st.phases[phase - 1] if 1 <= phase <= 3 else None
        age = time.time() - st.phase_ts
    if value is None or age > GRID_MAX_AGE:
        return None
    own = dps.get("137")
    if not isinstance(own, (int, float)):
        return None
    return value - own


def accept_meter_value(val, source=""):
    """Checks a meter reading and takes it into the state.

    Used by both sources (HTTP poll and MQTT topic) so the controller sees the
    same filtering either way.

    Two stages:
      1. Obvious nonsense (not finite, beyond ECO_ABSURD) is dropped straight
         away.
      2. A jump larger than ECO_SPIKE against the last valid reading is only
         accepted once the next reading confirms it. A single outlier would
         otherwise drive the controller to its limit.

    Returns True if the reading was accepted.
    """
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return False
    val = float(val)

    if not math.isfinite(val) or abs(val) > ECO_ABSURD:
        log.warning("Meter reading %.0f W implausible - discarded%s", val,
                    f" ({source})" if source else "")
        return False

    message = None      # Ereignis, nach dem Lock abzusetzen
    jump = None       # set while waiting for confirmation
    with st.lock:
        previous = st.grid
        suspect = st.grid_suspect
        if (ECO_SPIKE and previous is not None
                and abs(val - previous) > ECO_SPIKE):
            if suspect is None or abs(val - suspect) > ECO_SPIKE:
                st.grid_suspect = val
                jump = previous
            else:
                message = (f"Meter jump confirmed: {previous:+.0f} -> "
                           f"{val:+.0f} W")
        elif suspect is not None:
            message = (f"Outlier discarded: {suspect:+.0f} W "
                       f"(meter stayed at {val:+.0f} W)")
        if jump is None:
            st.grid_suspect = None
            st.grid = val
            st.grid_ts = time.time()

    # event() takes st.lock itself - hence down here, not in the block above.
    if jump is not None:
        log.info("Meter jump %.0f -> %.0f W - waiting for confirmation",
                 jump, val)
        return False
    if message:
        event(message, also_log=False)
    mqttc.publish(f"{BASE}/grid", int(val))
    return True


def eco_loop():
    """Polls the Eco Tracker directly over HTTP - independent of HA."""
    fails = 0
    while True:
        try:
            with urllib.request.urlopen(ECO_URL, timeout=5) as resp:
                data = json.loads(resp.read().decode())

            val = data.get(ECO_FIELD)
            if val is None:
                raise ValueError(f"field {ECO_FIELD!r} missing from the reply")

            accept_phases(data)
            log_phases()

            # agePower is the age of the reading in ms. A very old value means
            # the tracker itself is no longer receiving data.
            age = data.get("agePower")
            if isinstance(age, (int, float)) and age > 30000:
                log.warning("Eco Tracker is returning stale data (%.0f ms)", age)
                fails += 1
                time.sleep(ECO_INTERVAL)
                continue

            val = float(val)

            if not accept_meter_value(val, "HTTP"):
                time.sleep(ECO_INTERVAL)
                continue

            if fails >= 5:
                event("Eco Tracker reachable again", also_log=False)
            fails = 0

        except Exception as exc:
            fails += 1
            if fails in (1, 5) or fails % 20 == 0:
                log.warning("Eco Tracker unreachable (%dx): %s", fails, exc)
                if fails == 5:
                    event("Eco Tracker is not responding", also_log=False)

        time.sleep(ECO_INTERVAL)


# --------------------------------------------------------------------------
# Control loop
# --------------------------------------------------------------------------

def shelly_rpc(ip, path_):
    """Calls a Shelly's RPC interface (Gen2/Gen3)."""
    with urllib.request.urlopen(f"http://{ip}/rpc/{path_}", timeout=4) as resp:
        return json.loads(resp.read().decode())


def shelly_status(ip):
    """Reads power and relay state. Gen2/Gen3 first, then Gen1."""
    try:
        d = shelly_rpc(ip, "Switch.GetStatus?id=0")
        return d.get("apower"), d.get("output")
    except Exception:
        pass
    # Gen1: /status returns meters[0].power and relays[0].ison
    with urllib.request.urlopen(f"http://{ip}/status", timeout=4) as resp:
        d = json.loads(resp.read().decode())
    power = (d.get("meters") or [{}])[0].get("power")
    on = (d.get("relays") or [{}])[0].get("ison")
    return power, on


def shelly_switch_raw(ip, on):
    """Switches a Shelly. Gen2/Gen3 first, then Gen1."""
    try:
        shelly_rpc(ip, f"Switch.Set?id=0&on={'true' if on else 'false'}")
        return
    except Exception:
        pass
    url = f"http://{ip}/relay/0?turn={'on' if on else 'off'}"
    with urllib.request.urlopen(url, timeout=4):
        pass


def shelly_slot(nr):
    """Returns the state prefix, label and MQTT topic for one Shelly slot."""
    slots = {
        1: ("shelly", "Shelly"),
        2: ("shelly2", "Shelly north PV"),
        3: ("ac_storage1", "AC storage 1"),
        4: ("ac_storage2", "AC storage 2"),
    }
    key, name = slots[nr]
    return key, name, f"{BASE}/{key}"


def shelly_poll(nr):
    """Reads power and relay state of one configured Shelly."""
    key, name, topic = shelly_slot(nr)
    ip = getattr(st, f"{key}_ip")
    if not ip:
        return
    try:
        power, on = shelly_status(ip)
        with st.lock:
            setattr(st, f"{key}_power",
                    float(power) if power is not None else None)
            setattr(st, f"{key}_on", bool(on) if on is not None else None)
            if hasattr(st, f"{key}_ts") and power is not None:
                setattr(st, f"{key}_ts", time.time())
        fails = getattr(st, f"{key}_fails")
        if fails >= 5:
            event(f"{name} reachable again ({ip})", also_log=False)
        setattr(st, f"{key}_fails", 0)
        mqttc.publish(topic, json.dumps({
            "power": round(power) if power is not None else None,
            "state": "ON" if on else "OFF",
            "ip": ip,
        }))
    except Exception as exc:
        setattr(st, f"{key}_fails", getattr(st, f"{key}_fails") + 1)
        fails = getattr(st, f"{key}_fails")
        if fails in (1, 5) or fails % 60 == 0:
            log.warning("%s %s unreachable (%dx): %s", name, ip, fails, exc)
        if fails == 5:
            event(f"{name} {ip} is not responding", also_log=False)


def shelly_loop(nr):
    """Polls one Shelly independently so a failed unit delays no other."""
    while True:
        shelly_poll(nr)
        time.sleep(SHELLY_INTERVAL)


def shelly_switch(on, nr=1):
    """Switches a Shelly. Hard-disconnects that device from mains."""
    key, name, _topic = shelly_slot(nr)
    ip = getattr(st, f"{key}_ip")
    if not ip:
        log.warning("%s has no Shelly IP configured", name)
        return False
    try:
        shelly_switch_raw(ip, on)
        with st.lock:
            setattr(st, f"{key}_on", bool(on))
            if not on and hasattr(st, f"{key}_ts"):
                setattr(st, f"{key}_power", 0.0)
                setattr(st, f"{key}_ts", time.time())
        event(f"{name} {'switched on' if on else 'switched off'} ({ip})")
        return True
    except Exception as exc:
        log.error("Switching %s failed: %s", name, exc)
        return False


def offgrid_watch_loop():
    """Reports for as long as the state of charge locks out the off-grid
    output.

    No datapoint reveals the socket's actual state - DP 135 reports about
    235 V throughout, whether or not anything is present at the output. The
    lockout is therefore the only statement that holds up: below the discharge
    stop plus OFFGRID_SOC_MARGIN the device will not release the output, no
    matter what DP 119 says.
    """
    while True:
        time.sleep(20)
        if not st.online:
            continue
        dps = st.snapshot()
        if not dps.get(DP_OFFGRID):
            st.offgrid_reported = False
            continue                      # not enabled, nothing to report
        locked, soc, threshold = offgrid_locked(dps)
        if locked and not st.offgrid_reported:
            st.offgrid_reported = True
            event(f"Off-grid output enabled but locked out: state of charge "
                  f"{soc} %, the device releases it at {threshold} %")
        elif not locked and st.offgrid_reported:
            st.offgrid_reported = False
            event(f"Off-grid output released again (state of charge {soc} %)")


def charge_guard_loop():
    """Holds the battery charge limit (DP 122) at the configured value.

    DP 122 caps the battery's total charge power - from PV *and* from the
    grid. At 0 the device switches its MPPT controllers off and takes in no
    solar power at all, even on on empty battery in full sun. The value must
    therefore stay up permanently.

    Incidentally, DP 118 (backflow prevention) blocks export to the grid, not
    charging from the grid - the name is misleading.
    """
    while True:
        time.sleep(30)
        if not st.online:
            continue
        setpoint = st.tune["charge_limit"]
        actual = st.snapshot().get(DP_CHARGE)
        if isinstance(actual, int) and actual != setpoint:
            log.info("Battery charge limit is %s W instead of %s W - corrected",
                     actual, setpoint)
            set_charge(setpoint, reason="re-enable PV charging")


def set_pv_limit(watt, reason=""):
    """Writes DP 156, the PV charge power.

    This throttles the solar side without touching the export limit. Careful:
    the device also adjusts this value on its own if a PV schedule is
    configured in the app.
    """
    return write_dp(DP_PV_LIMIT, watt, 4000, reason)


def neighbour_discharging(now=None):
    """Is a neighbouring storage unit currently discharging?

    Returns (discharging, known). Without a configured or fresh reading
    ``known`` is False, and the caller has to decide how careful to be.
    """
    now = now or time.time()
    if not NEIGHBOUR_TOPIC:
        return False, False
    with st.lock:
        power, timestamp = st.neighbour_power, st.neighbour_ts
    if power is None or now - timestamp > NEIGHBOUR_MAX_AGE:
        return False, False
    if NEIGHBOUR_SIGN == "charge_positive":
        power = -power
    return power > NEIGHBOUR_DISCHARGE_W, True


def set_mode(mode, reason=""):
    """Switches the operating mode and verifies the device followed.

    DP 165 is the slot that takes effect; DP 169 is written as well so the
    app shows a consistent picture. The reply frame cannot be trusted - in
    testing a write to 165 was acknowledged with an unrelated datapoint - so
    DP 117 is read back a few seconds later.
    """
    st.mode_wanted = mode
    ok = write_dp_raw(DP_MODE_SLOT1, mode, reason)
    write_dp_raw(DP_MODE_SLOT5, mode, reason)
    # Verify even when the write was reported as rejected. Twice now a "no
    # response" was followed a second later by the device reporting the new
    # value - the command landed, only the acknowledgement was lost. Skipping
    # the read-back there would leave the switch unverified precisely when it
    # matters most.
    threading.Timer(MODE_VERIFY_DELAY, verify_mode, (mode,)).start()
    return ok


def verify_mode(expected):
    """Reads DP 117 back and reports when the device did not follow."""
    dps = read_device_status()
    if dps is None or DP_MODE not in dps:
        return
    actual = dps[DP_MODE]
    if actual != expected:
        event(f"Operating mode: device reports {actual}, requested "
              f"{expected} - retrying")
        write_dp_raw(DP_MODE_SLOT1, expected, "mode retry")
    # Slot 5 is cosmetic - only slot 1 decides - but it was seen reverting on
    # its own after a write, which leaves the app showing a schedule that is
    # not in effect. Put it back quietly.
    if dps.get(DP_MODE_SLOT5) not in (None, expected):
        log.info("DP %s drifted back to %r, correcting",
                 DP_MODE_SLOT5, dps[DP_MODE_SLOT5])
        write_dp_raw(DP_MODE_SLOT5, expected, "slot 5 resync")
    publish_state()


def write_dp_raw(dp, value, reason=""):
    """Writes a non-numeric datapoint. Returns True when accepted."""
    res = tuya_call("set_value", int(dp), value)
    ok, reason_txt = response_ok(res)
    if ok:
        st.last_write = time.time()
        st.merge({dp: value})
        log.info("DP %s -> %r (%s)", dp, value, reason or "control")
        return True
    log.warning("DP %s -> %r rejected: %s", dp, value, reason_txt)
    return False


def probe_verdict(meter_now, draw_now, now=None):
    """Judges a running probe. Returns (verdict, detail).

    verdict is "wait" while the step is still settling, "genuine" when the
    meter followed our own change, "compensated" when something else made up
    for it, and "unclear" when the device did not actually change its draw.
    """
    now = now or time.time()
    if not st.probe_at:
        return "wait", "no probe running"
    if now - st.probe_at < PROBE_SETTLE:
        return "wait", "settling"
    if st.probe_meter is None or st.probe_draw is None:
        return "unclear", "no reference taken"
    if not isinstance(meter_now, (int, float)) or not isinstance(draw_now, (int, float)):
        return "unclear", "no reading"

    delta_draw = draw_now - st.probe_draw
    delta_meter = meter_now - st.probe_meter
    if delta_draw < PROBE_MIN_DRAW:
        return "unclear", f"device only changed by {delta_draw:.0f} W"
    share = delta_meter / delta_draw
    detail = (f"drew {delta_draw:+.0f} W, meter moved {delta_meter:+.0f} W "
              f"({share * 100:.0f} %)")
    if share >= PROBE_ACCEPT:
        return "genuine", detail
    return "compensated", detail


def surplus_decision(grid, soc, socmax, draw, now=None):
    """Decides what AC surplus charging should do this cycle.

    Pure function, so the whole state machine can be exercised without a
    device. Returns (action, watts, reason); action is one of "enter",
    "probe", "hold", "lower", "leave" or "idle".

    Only increases are probed. Lowering is immediate and never needs
    permission - that keeps the loop safe by construction and calm by
    default: at most one step up per PROBE_SETTLE seconds.
    """
    now = now or time.time()

    def leave(why):
        st.surplus_since = 0.0
        st.surplus_gone = 0.0
        st.probe_at = 0.0
        return "leave", 0, why

    if isinstance(soc, (int, float)) and isinstance(socmax, (int, float)):
        if soc >= socmax:
            return leave("battery full")
    if grid is None:
        return leave("no meter reading")

    # The neighbour reading stays optional. Where it exists it saves the
    # probing entirely, because the answer is already known.
    discharging, known = neighbour_discharging(now)
    if known and discharging:
        return leave("neighbouring battery is discharging")

    surplus = -grid

    if not st.surplus_active:
        if surplus < SURPLUS_ENTER_W:
            st.surplus_since = 0.0
            return "idle", 0, "not enough surplus"
        if not st.surplus_since:
            st.surplus_since = now
            return "idle", 0, "surplus seen, waiting for confirmation"
        if now - st.surplus_since < SURPLUS_ENTER_S:
            return "idle", 0, "surplus confirming"
        st.surplus_gone = 0.0
        # The first step is a probe like any other, and capped like any
        # other. Jumping straight to the full surplus would put several
        # hundred unverified watts on the grid before the first check.
        first = int(min(surplus, SURPLUS_STEP, CHARGE_HW_MAX))
        return "enter", first, f"surplus confirmed, starting at {first} W"

    # Importing: come down at once, no probe, no waiting.
    if grid > 0:
        st.probe_at = 0.0
        wanted = max(0, st.charge_setpoint - grid)
        if wanted < SURPLUS_MIN_W:
            if not st.surplus_gone:
                st.surplus_gone = now
            elif now - st.surplus_gone >= SURPLUS_EXIT_S:
                return leave("surplus gone")
            return "lower", int(max(0, wanted)), "importing, backing off"
        return "lower", int(wanted), "importing, backing off"

    st.surplus_gone = 0.0

    # A probe in flight has to be judged before anything else happens.
    verdict, detail = probe_verdict(st.phase_sum, draw, now)
    if verdict == "wait":
        if st.probe_at:
            return "hold", int(st.charge_setpoint), "probe settling"
    elif verdict == "compensated":
        st.probe_at = 0.0
        return leave(f"another source compensated - {detail}")
    elif verdict in ("genuine", "unclear"):
        st.probe_at = 0.0

    if surplus < SURPLUS_MIN_W:
        return "hold", int(st.charge_setpoint), "balanced"

    step = int(min(SURPLUS_STEP, surplus))
    wanted = int(min(CHARGE_HW_MAX, st.charge_setpoint + step))
    if wanted == st.charge_setpoint:
        return "hold", int(st.charge_setpoint), "at the limit"
    return "probe", wanted, f"testing {step} W more"


def start_backup():
    """Charges the battery from the grid until it is full. Manual only."""
    st.backup_on = True
    st.surplus_active = False
    stop_passthrough("backup charging", safe_limit=LIMIT_SAFE)
    if st.charge_before is None:
        st.charge_before = st.dps.get(DP_CHARGE, st.tune["charge_limit"])
    set_charge(st.backup_power, reason="backup charge")
    set_mode(MODE_BACKUP, reason="backup charge")
    event(f"Backup charging started at {st.backup_power} W - control paused")


def stop_charging(reason):
    """Leaves grid charging and restores everything it changed."""
    was_active = st.surplus_active or st.backup_on
    st.surplus_active = False
    st.charge_setpoint = 0.0
    if st.mode_wanted != MODE_GRID:
        set_mode(MODE_GRID, reason=reason)
    if st.charge_before is not None:
        set_charge(st.charge_before, reason=reason)
        st.charge_before = None
    if was_active:
        event(f"Grid charging ended ({reason})")
    publish_state()


def stop_passthrough(reason, safe_limit=None):
    """Ends pass-through and restores every limit it may have changed."""
    was_active = st.pass_active
    needs_restore = (was_active or st.pass_pv_pending or st.pv_reopen
                     or st.pass_pv_setpoint > 0)
    st.pass_active = False
    st.pass_pv_pending = False
    st.pass_pv_setpoint = 0
    st.pass_next = 0.0

    if needs_restore:
        st.pv_reopen = not set_pv_limit(
            st.tune["pv_max"], reason=f"pass-through ended: {reason}")
    if safe_limit is not None:
        set_limit(safe_limit, reason=reason)
        st.setpoint = float(safe_limit)
    if was_active:
        event(f"Pass-through ended ({reason})")
    publish_state()


def offgrid_locked(dps):
    """Checks whether the state of charge locks out the off-grid output.

    Returns (locked, state_of_charge, threshold). Without usable values
    nothing counts as locked - better no report than a wrong one.
    """
    soc = dps.get("102")
    soc_min = dps.get(DP_SOC_MIN)
    if not isinstance(soc, (int, float)) or not isinstance(soc_min, (int, float)):
        return False, soc, None
    threshold = soc_min + OFFGRID_SOC_MARGIN
    return soc < threshold, soc, threshold


def read_device_status():
    """Fetches a full status and merges it. Returns dps, or None."""
    data = tuya_call("status")
    if not (isinstance(data, dict) and "dps" in data):
        log.warning("Status query failed: %s", data)
        return None
    log_changes(st.merge(data["dps"]), "(verify)")
    return data["dps"]


def verify_switch(dp, expected, name):
    """Reads back whether the device carried out a switch command.

    The device acknowledges commands with OK even when it then does not
    execute them or reverts immediately. Without the read-back the requested
    value would sit in the cache and therefore in Home Assistant while
    something else is true at the device - exactly the case seen on 11.09.:
    socket off at the device, on in HA.
    """
    dps = read_device_status()
    if dps is None or dp not in dps:
        return
    actual = bool(dps[dp])
    if actual != bool(expected):
        event(f"{name}: device reports "
              f"{'on' if actual else 'off'}, requested was "
              f"{'on' if expected else 'off'} - display corrected")
    publish_state()


def set_backflow(block, source=""):
    """Switches backflow prevention (DP 118).

    The name refers to backflow *into the grid*: switched on it blocks export
    entirely and the device goes to standby. It has no effect on charging
    from the grid.
    """
    res = tuya_call("set_value", int(DP_BACKFLOW), bool(block))
    ok, reason_txt = response_ok(res)
    if not ok:
        log.warning("Backflow prevention rejected: %s", reason_txt)
        return False
    st.merge({DP_BACKFLOW: bool(block)})
    log.info("Export %s%s", "blocked" if block else "allowed",
             f" ({source})" if source else "")
    return True


# Bits of the DP 149 system fault register. The app shows nothing but the bare
# number, labelled "system fault". Bit 7 is pinned down by on experiment: a
# deliberately induced overload on the AC output set the value to 128, the
# device dropped the grid side, and the value stayed put until the device was
# restarted.
#
# That this is a bit field is on educated guess, not a proven fact: only 2 and
# 128 have been observed, both powers of two. A combined value such as 130
# would settle it. Unknown bits are therefore reported individually and
# honestly as unknown.
# Keys are bit values, not bit numbers.
FEHLER_BITS = {
    128: "AC output overload",   # Bit 7
}
# Bits that occur in undisturbed operation and are not faults.
FAULT_HARMLESS = {2}                  # Bit 1, wechselt zusammen mit 101/114


def fault_text(value):
    """Breaks DP 149 down into the bits that are set.

    Returns (text, serious). ``serious`` is False as long as only bits known
    to be harmless are set - otherwise the register's normal flicker would
    raise on alarm constantly.
    """
    if not isinstance(value, int) or value == 0:
        return "no fault", False
    parts, serious = [], False
    for bit in range(16):
        mask = 1 << bit
        if not value & mask:
            continue
        if mask in FAULT_HARMLESS:
            continue
        serious = True
        parts.append(FEHLER_BITS.get(mask,
                                     f"unknown bit {bit} ({mask})"))
    if not parts:
        return "no fault", False
    return ", ".join(parts), serious


def control_loop():
    """Controls export through DP 121.

    One direction only: if the meter shows import, the device gives more; if
    there is surplus, the export limit is pulled back and PV charges the
    battery by itself. No actuator for charge power is needed for that -
    DP 122 stays open permanently, see charge_guard_loop().

    Above "Pass-through from SoC" a second, entirely different branch takes
    over: it no longer regulates against the meter but holds the state of
    charge and exports a constant figure. See the comments there.
    """
    while True:
        time.sleep(st.tune["interval"])

        if not st.online:
            continue

        dps = st.snapshot()
        limit = dps.get(DP_LIMIT)
        if limit is None:
            continue

        # ------------------------------------------------------------------
        # Priority: backup > pass-through > AC surplus > zero-export
        # ------------------------------------------------------------------
        # Backup was asked for by hand, so it wins over everything. It stops
        # by itself at the charge stop, because leaving the device sitting at
        # 100 % costs harvest for hours.
        if st.backup_on:
            soc_now, soc_max = dps.get("102"), dps.get(DP_SOC_MAX)
            if (isinstance(soc_now, (int, float))
                    and isinstance(soc_max, (int, float))
                    and soc_now >= soc_max):
                st.backup_on = False
                stop_charging(f"battery full at {soc_now} %")
                mqttc.publish(f"{BASE}/backup/state", "OFF", retain=True)
            continue

        # AC surplus charging and a negative meter target cancel each other
        # out: one deliberately creates export, the other absorbs it. Running
        # both is a loop, so it is refused rather than silently prioritised.
        if st.surplus_on and st.correction < 0:
            if not st.surplus_active:
                st.surplus_on = False
                event("AC surplus charging disabled - it cannot run "
                      "alongside a negative meter target")
                mqttc.publish(f"{BASE}/surplus/state", "OFF", retain=True)

        if st.surplus_on and not st.pass_active:
            with st.lock:
                smoothed = st.phase_sum
            action, watt, why = surplus_decision(
                smoothed, dps.get("102"), dps.get(DP_SOC_MAX), dps.get("137"))
            if action in ("enter", "probe", "lower", "hold"):
                if not st.surplus_active:
                    stop_passthrough("surplus charging", safe_limit=LIMIT_SAFE)
                    st.surplus_active = True
                    if st.charge_before is None:
                        st.charge_before = dps.get(DP_CHARGE,
                                                   st.tune["charge_limit"])
                    set_mode(MODE_BACKUP, reason="surplus charging")
                    event(f"Taking AC surplus - {why}")
                if action in ("enter", "probe"):
                    with st.lock:
                        st.probe_meter, st.probe_draw = smoothed, dps.get("137")
                        st.probe_at = time.time()
                if watt != int(st.charge_setpoint):
                    st.charge_setpoint = watt
                    with st.lock:
                        ph = list(st.phases)
                    log.info("Surplus | sum %s W | L1 %s L2 %s L3 %s | "
                             "charge -> %s W (%s)",
                             None if smoothed is None else round(smoothed),
                             *[("?" if v is None else round(v)) for v in ph],
                             watt, why)
                    set_charge(watt, reason="surplus charging")
                    publish_state()
                continue
            if action == "leave" and st.surplus_active:
                event(f"AC surplus charging stopped - {why}")
                stop_charging(why)
        elif st.surplus_active:
            stop_charging("superseded")

        if not st.control_on:
            continue

        # Every mode that feeds the grid needs a fresh meter value. Keeping
        # the last export command alive during a meter outage would mean
        # exporting blind, including while pass-through is active.
        with st.lock:
            grid = st.grid
            grid_age = time.time() - st.grid_ts
            requested_target = st.correction

        if grid is None or grid_age > GRID_MAX_AGE:
            if st.pass_active:
                stop_passthrough("meter failure")
            if not st.grid_fail:
                st.grid_fail = True
                event(f"Meter is not reporting - export limit set to "
                      f"{GRID_FAIL_LIMIT} W")
                set_limit(GRID_FAIL_LIMIT, reason="meter failure")
                st.setpoint = float(GRID_FAIL_LIMIT)
                publish_state()
            continue
        if st.grid_fail:
            st.grid_fail = False
            event("Meter is reporting again - control active")

        storage_power, storage_ready, storage_configured = \
            ac_storage_charge_power()
        meter_target = compensated_meter_target(
            requested_target, storage_power, storage_ready,
            storage_configured)
        waiting = (requested_target < 0 and storage_configured
                   and not storage_ready)
        if waiting and not st.ac_storage_wait:
            st.ac_storage_wait = True
            event("Negative meter target paused - no fresh reading from an "
                  "enabled AC-storage Shelly")
        elif not waiting and st.ac_storage_wait:
            st.ac_storage_wait = False
            event("AC-storage Shelly readings available - negative target active")

        # ------------------------------------------------------------------
        # Pass-through
        # ------------------------------------------------------------------
        # Goal: the state of charge stays put and the device exports a
        # constant LIMIT_MAX into the house. Because PV always runs through
        # the battery on the EP2500, that simply means setting the PV charge
        # power so it covers the export. Two fixed values, no balance loop.
        #
        # Only the conversion loss is trimmed, and it is trimmed against the
        # state of charge. The loss works in the safe direction: 800 W into
        # the battery yields less than 800 W at the output, so the state of
        # charge drifts down on its own. Overshoots are the exception rather
        # than the rule - hence one small step every few minutes is enough.
        soc = dps.get("102")
        target = st.tune["soc_pass"]
        if st.pass_on and isinstance(soc, (int, float)):
            # Hysteresis: once active it stays until PASS_HYST below target
            threshold = target - PASS_HYST if st.pass_active else target
            reached = soc >= threshold
            if requested_target == 0:
                active = reached
            else:
                # A negative or positive meter target normally wins: the user
                # asked for a specific figure at the meter, and pass-through
                # would override it with a fixed LIMIT_MAX.
                #
                # That deference ends near the charge stop. Letting the device
                # run into its 100 % shutdown costs far more than missing a
                # meter target for a while, and the protection was silently
                # off here before - the check used the requested target, so
                # even a target the AC-storage compensation had already
                # neutralised to 0 disabled it.
                # Hysteresis on the override as well: it engages PASS_OVERRIDE
                # points above the target and only lets go once the state of
                # charge is back at the target. Without that it would chatter
                # on and off across a single point.
                override_at = target if st.pass_active else target + PASS_OVERRIDE
                active = reached and soc >= override_at
                if active and not st.pass_active:
                    event(f"Meter target {requested_target:+d} W suspended - "
                          f"state of charge {soc} % is within "
                          f"{PASS_OVERRIDE} points of the pass-through target")
                elif reached and not active and not st.pass_soc_warned:
                    st.pass_soc_warned = True
                    event(f"Pass-through held back by meter target "
                          f"{requested_target:+d} W at {soc} % - protection "
                          f"takes over at {target + PASS_OVERRIDE} %")
            if not reached:
                st.pass_soc_warned = False
        else:
            active = False
            st.pass_soc_warned = False

        if active != st.pass_active:
            if active:
                st.pass_active = True
                st.pass_next = time.time() + PASS_SETTLE
                event(f"Pass-through active - holding state of charge at {target} %, "
                      f"{LIMIT_MAX} W into the house")
                set_limit(LIMIT_MAX, reason="pass-through")
                st.setpoint = float(LIMIT_MAX)
                # Same rule here: only advance the marker on a successful
                # write. If the command is rejected, the controller would
                # otherwise start out holding a value the device never got.
                if set_pv_limit(LIMIT_MAX, reason="pass-through"):
                    st.pass_pv_setpoint = LIMIT_MAX
                    st.pass_pv_pending = False
                else:
                    st.pass_pv_pending = True
                    st.pass_next = time.time() + PASS_ADJUST_FAST
            else:
                reason = ("signed meter target active"
                          if requested_target != 0
                          else "state of charge below threshold")
                stop_passthrough(reason)
            publish_state()

        # Catch up if reopening the PV limit failed when pass-through ended.
        if st.pv_reopen and not active:
            if set_pv_limit(st.tune["pv_max"],
                            reason="pass-through ended (retry)"):
                st.pv_reopen = False
                publish_state()

        if active:
            # The initial PV-limit write may have failed. Retry it even while
            # SoC is exactly on target; otherwise the old code could remain in
            # pass-through forever with no effective PV setting.
            if st.pass_pv_pending:
                if set_pv_limit(LIMIT_MAX,
                                reason="pass-through: initial retry"):
                    st.pass_pv_setpoint = LIMIT_MAX
                    st.pass_pv_pending = False
                    st.pass_next = time.time() + PASS_SETTLE
                    publish_state()
                else:
                    st.pass_next = time.time() + PASS_ADJUST_FAST
                continue

            # The export limit is fixed. If something else moved it (the app,
            # a rejected command), put it back.
            if isinstance(limit, int) and limit != LIMIT_MAX:
                log.info("Pass-through | export limit is %s W instead of %s W - "
                         "corrected", limit, LIMIT_MAX)
                set_limit(LIMIT_MAX, reason="pass-through: hold limit")
                st.setpoint = float(LIMIT_MAX)
                publish_state()

            if time.time() < st.pass_next:
                continue

            deviation = int(soc) - target
            if deviation == 0:
                # State of charge is on target - do nothing.
                st.pass_next = time.time() + PASS_ADJUST
                continue

            # Step size proportional to the deviation. The symmetry matters:
            # down and up at the same rate. Lowering fast and recovering
            # slowly would drive the state of charge further down with every
            # passing cloud.
            step = max(-PASS_STEP_FAST,
                          min(PASS_STEP_FAST, -deviation * PASS_STEP))
            wait = (PASS_ADJUST_FAST if abs(deviation) >= PASS_ALARM
                         else PASS_ADJUST)
            st.pass_next = time.time() + wait

            # Battery gate: stop adjusting once the battery is already
            # working in the desired direction. The state of charge lags the
            # power by minutes. Without this brake the controller keeps
            # pushing during the lag and overshoots badly - that is how the PV
            # setpoint once ran from 725 down to 0 while charging had already
            # almost stopped.
            #
            # Battery power is only a sign check here, not a control variable.
            # Its 20 to 70 s delay does not matter at a cadence of 45 s or
            # more.
            batt = dps.get(DP_BATT)
            target_batt = -deviation * PASS_BATT_BIAS
            if isinstance(batt, (int, float)):
                if step < 0 and batt <= target_batt:
                    continue
                if step > 0 and batt >= target_batt:
                    continue

            # Anti-windup: only raise the setpoint when the limit is actually
            # the binding constraint. If PV delivers less than allowed, the
            # cloud is the cause and not the setting - turning it up further
            # changes nothing, and the state of charge would overshoot the
            # moment the sun returns.
            pv_actual = dps.get("143")
            if (step > 0 and isinstance(pv_actual, (int, float))
                    and pv_actual < st.pass_pv_setpoint - PASS_STEP):
                continue

            ceiling = min(PASS_PV_CAP, st.tune["pv_max"])
            new = max(0, min(ceiling, st.pass_pv_setpoint + step))
            if new == st.pass_pv_setpoint:
                continue
            log.info("Pass-through | SoC %s %% (target %s) | battery %s W | "
                     "PV charge power %s -> %s W",
                     soc, target, batt, st.pass_pv_setpoint, new)
            if set_pv_limit(new, reason="pass-through: hold SoC"):
                st.pass_pv_setpoint = new
                publish_state()
            else:
                # Rejected: the device did not take the value. Leave the
                # marker alone, otherwise the controller would carry on from a
                # figure that was never set and its assumption would drift
                # away from reality. Retry soon.
                st.pass_next = time.time() + PASS_ADJUST_FAST
            continue

        ac_out = dps.get(DP_AC_OUT)
        status = dps.get("134")

        actual = float(ac_out) if isinstance(ac_out, (int, float)) else 0.0
        target = meter_target
        error = grid - target          # >0 = importing, device must give more

        # Idle lock: only when power was actually requested and nothing comes
        # out anyway (empty battery). If the limit is at 0 because the
        # controller put it there itself, do not pause - otherwise it would
        # never start up again.
        idle = (is_standby(status) and actual == 0.0 and limit >= 20
                and error > 0)
        if idle:
            if not st.idle_logged:
                event("Control paused - device in standby, "
                      f"battery {dps.get('102', '?')} %")
                st.idle_logged = True
            st.idle_since = time.time()
            continue
        if st.idle_logged:
            # Only resume once the device has been out of standby for
            # IDLE_HOLD without interruption. A brief flicker is not enough -
            # otherwise control alternates between paused and active every
            # minute and fills the event log.
            if time.time() - st.idle_since < IDLE_HOLD:
                continue
            event("Control resumed - device responding")
            st.idle_logged = False

        if abs(error) <= st.tune["deadband"]:
            continue

        # Anti-windup: the setpoint must not run beyond what the device can
        # actually deliver. If PV only yields 300 W, a setpoint of 800 W is a
        # number without effect that would take hold abruptly the moment the
        # sun returns.
        #
        # Clamp upwards only. A lower clamp (setpoint at least actual minus
        # margin) did damage here for a long time: DP 155 arrives roughly
        # every 20 s while the controller runs every few seconds. Right after
        # a reduction, "actual" still shows the old, high output, and the
        # lower clamp undid the correction that had just been made. Observed:
        # at a meter deviation of -15 W the setpoint jumped from 432 to 720 W
        # because "actual" still read 795 W.
        max_step = st.tune["max_step"]
        setpoint = calculate_setpoint(grid, target, actual, st.setpoint,
                                      st.tune["gain"], max_step)
        st.setpoint = setpoint

        new = int(round(setpoint))
        if abs(new - limit) < CTRL_MIN_STEP:
            continue

        log.info("Meter %+.0f W (target %+.0f, requested %+d, AC storage %.0f W) "
                 "| actual %.0f W | export %s -> %s",
                 grid, target, requested_target, storage_power,
                 actual, limit, new)
        set_limit(new, reason="control")
        publish_state()


def main():
    if GRID_SOURCE not in {"http", "mqtt"}:
        log.error("GRID_SOURCE must be 'http' or 'mqtt', not %r.", GRID_SOURCE)
        raise SystemExit(1)

    missing = [n for n, v in (("EP2500_ID", DEVICE_ID),
                              ("EP2500_IP", DEVICE_IP),
                              ("EP2500_KEY", LOCAL_KEY)) if not v]
    if missing:
        log.error("Please set %s in the environment (see README).",
                  ", ".join(missing))
        raise SystemExit(1)

    # connect_async does not block: the bridge starts even when the broker is
    # not reachable yet and connects itself later.
    mqttc.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqttc.loop_start()

    threading.Thread(target=tuya_loop, daemon=True).start()
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=charge_guard_loop, daemon=True).start()
    threading.Thread(target=offgrid_watch_loop, daemon=True).start()
    for nr in (1, 2, 3, 4):
        threading.Thread(target=shelly_loop, args=(nr,), daemon=True).start()
    if not REC_HOST:
        log.warning("REC_HOST is not set - recordings still work, but the "
                    "dashboard cannot offer a download link. Set it to the "
                    "address of this machine, port %s.", REC_PORT)
    threading.Thread(target=record_server_loop, daemon=True).start()
    threading.Thread(target=record_info_loop, daemon=True).start()

    if GRID_SOURCE == "http":
        log.info("Meter source: HTTP %s (field %s)", ECO_URL, ECO_FIELD)
        threading.Thread(target=eco_loop, daemon=True).start()
    else:
        log.info("Meter source: MQTT %s", GRID_TOPIC)

    shutdown_evt = threading.Event()

    def handle_signal(signum, frame):
        log.info("Signal %s received", signum)
        shutdown_evt.set()

    # docker stop sends SIGTERM - without handling it, cleanup never runs.
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    shutdown_evt.wait()

    log.info("Shutting down, setting export limit to %s W", LIMIT_SAFE)
    if st.pass_active:
        # Reopen any PV throttling that pass-through left in place
        set_pv_limit(st.tune["pv_max"], reason="Shutdown")
    set_limit(LIMIT_SAFE, reason="Shutdown")
    mqttc.publish(f"{BASE}/available", "offline", retain=True)
    time.sleep(1)
    mqttc.loop_stop()


if __name__ == "__main__":
    main()
