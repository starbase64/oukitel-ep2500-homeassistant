#!/usr/bin/env python3
"""
EP2500 <-> MQTT Bridge mit Home-Assistant-Discovery und Nulleinspeisungs-Regler.

Aufgaben:
  1. Persistente lokale Tuya-Verbindung zum Oukitel EP2500 (Protokoll 3.5).
     Das Geraet sendet nach dem ersten Vollstatus nur noch Delta-Frames,
     deshalb wird ein Cache gefuehrt und jedes Frame hineingemergt.
  2. Veroeffentlichung aller relevanten Werte per MQTT mit HA-Discovery.
  3. Regelkreis: fuehrt DP 121 (Einspeisegrenze) so nach, dass der
     Zaehlerwert vom Eco Tracker gegen den Korrekturwert laeuft.

Konfiguration ueber Umgebungsvariablen, Defaults siehe unten.

Abhaengigkeiten:  pip install tinytuya paho-mqtt
"""

import datetime
import json
import logging
import math
import signal
import os
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt
import tinytuya

# --------------------------------------------------------------------------
# Konfiguration
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

# Zaehlerwert (positiv = Netzbezug, negativ = Einspeisung).
# GRID_SOURCE "http"  -> Eco Tracker wird direkt abgefragt (Standard)
# GRID_SOURCE "mqtt"  -> Wert kommt von GRID_TOPIC
GRID_SOURCE = os.getenv("GRID_SOURCE", "http").lower()

ECO_URL = os.getenv("ECO_URL", "http://192.168.1.100/status")
ECO_FIELD = os.getenv("ECO_FIELD", "power")        # oder "powerAvg"
ECO_INTERVAL = int(os.getenv("ECO_INTERVAL", "5"))  # s

# Plausibilitaetspruefung: Ein Sprung groesser als ECO_SPIKE Watt gegenueber
# dem letzten Wert wird erst uebernommen, wenn die naechste Messung ihn
# bestaetigt. Das faengt einzelne Ausreisser ab, die den Regler sonst auf
# Anschlag jagen wuerden. 0 schaltet die Pruefung ab.
ECO_SPIKE = int(os.getenv("ECO_SPIKE", "600"))

# Messender Shelly vor dem Geraet - unabhaengige Kontrolle der
# tatsaechlichen AC-Leistung und harter Ein/Aus-Schalter.
SHELLY_IP = os.getenv("SHELLY_IP", "")
SHELLY_INTERVAL = int(os.getenv("SHELLY_INTERVAL", "5"))

# Zweiter Shelly, z. B. am Nord-Balkonkraftwerk. Leer lassen, wenn
# nicht vorhanden.
SHELLY2_IP = os.getenv("SHELLY2_IP", "")

# Nutzbare Kapazitaet fuer die Laufzeitschaetzung. Laut Typenschild
# 51,2 V x 40 Ah = 2048 Wh.
BATT_WH = int(os.getenv("BATT_WH", "2048"))

# Messender Shelly vor dem Geraet, dient als unabhaengige Gegenprobe zur
# AC-Ausgangsleistung des EP2500. Die Adresse ist in HA aenderbar.

# Zweiter Shelly, z. B. am Nord-Balkonkraftwerk. Leer lassen, wenn
# nicht vorhanden.

ECO_ABSURD = 30000          # W; alles darueber ist keine Messung mehr

GRID_TOPIC = os.getenv("GRID_TOPIC", "ecotracker/power")
GRID_JSON_KEY = os.getenv("GRID_JSON_KEY", "power")
GRID_MAX_AGE = 60          # s; aeltere Zaehlerwerte gelten als ungueltig

DP_LIMIT = "121"           # Einspeisegrenze, schreibbar
DP_CHARGE = "122"          # Netzladeleistung, schreibbar
DP_AC_OUT = "155"          # tatsaechliche AC-Ausgangsleistung
DP_BATT = "128"            # Batterieleistung, positiv = laden
DP_OFFGRID = "119"         # Off-Grid-Steckdose ein/aus, schreibbar
DP_BACKFLOW = "118"        # Rueckflussverhinderung: True = keine Einspeisung
DP_SOC_MAX = "123"         # Lade-Stopp-SoC in %
DP_SOC_MIN = "124"         # Entlade-Stopp-SoC in %
DP_PV_LIMIT = "156"        # PV-Ladeleistung, schreibbar

LIMIT_MIN = 0
LIMIT_MAX = int(os.getenv("LIMIT_MAX", "800"))   # gesetzliche Obergrenze
LIMIT_SAFE = int(os.getenv("LIMIT_SAFE", "300")) # Fallback beim Beenden
# Einspeisegrenze, auf die zurueckgefallen wird, wenn der Zaehler laenger
# als GRID_MAX_AGE keine Werte liefert. 0 stoppt die Einspeisung ganz,
# ein positiver Wert speist blind weiter. Die Batterieladegrenze bleibt
# davon unberuehrt, damit die PV weiter laden kann.
GRID_FAIL_LIMIT = int(os.getenv("GRID_FAIL_LIMIT", "0"))
# Obergrenze fuer die Batterieladegrenze (DP 122). Das Geraet nimmt laut
# Typenschild bis 4000 W ueber die vier MPPT-Eingaenge auf; der Akku selbst
# begrenzt bei 60 A, also rund 3000 W. 2500 W ist der Vorgabewert aus der
# App und deckt uebliche Anlagen ab.
CHARGE_HW_MAX = int(os.getenv("CHARGE_HW_MAX", "4000"))

# Regelparameter. Startwerte; zur Laufzeit ueber HA verstellbar und per
# retained MQTT gespeichert, ueberleben also einen Neustart der Bridge.
# TUNABLES: key -> (Anzeigename, min, max, step, Einheit, Typ)
TUNABLES = {
    "interval": ("Regelintervall",   3,   120, 1,   "s", int),
    "gain":     ("Reglerverstaerkung", 0.1, 2.0, 0.1, None, float),
    "deadband": ("Totband",          0,   200, 5,   "W", int),
    "max_step": ("Max. Schrittweite", 10,  800, 10,  "W", int),
    "charge_limit": ("Batterieladegrenze", 0, CHARGE_HW_MAX, 100, "W", int),
    "soc_pass":  ("Durchleitung ab SoC", 50, 100, 1, "%", int),
    "pv_max":    ("PV-Grenze offen", 500, 4000, 100, "W", int),
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

CTRL_MIN_STEP = 10         # W; kleinere Aenderungen werden nicht geschrieben
WINDUP_MARGIN = 60         # W; wie weit der Sollwert vom Istwert abweichen darf

# Durchleitung: Der EP2500 hat keinen direkten Pfad von PV zum Netz - die
# Einspeisung kommt immer aus dem Akku, und PV geht immer in den Akku. Ein
# stehender Ladestand heisst also: PV-Ladeleistung gleich Einspeiseleistung.
# Beide Werte werden deshalb fest gesetzt und nur noch traege nachgefuehrt.
#
# Geregelt wird auf den Ladestand, nicht auf die Batterieleistung. Der
# Ladestand ist genau die Groesse, die gehalten werden soll, und er ist
# traege. Die Batterieleistung trifft mit 20 bis 70 s Verzoegerung ein - als
# Regelgroesse fuehrt sie zu Schwingungen mit vollem Ausschlag.
PASS_HYST = 5              # %-Punkte unter dem Ziel endet die Durchleitung
PASS_STEP = 25             # W je normalem Nachfuehrschritt
PASS_STEP_FAST = 150       # W je Schritt im Alarmbereich
PASS_ADJUST = 180          # s zwischen zwei Nachfuehrschritten
PASS_ADJUST_FAST = 45      # s im Alarmbereich
PASS_ALARM = 3             # %-Punkte ueber Ziel = Alarmbereich
PASS_SETTLE = 120          # s Ruhe nach dem Start, bevor nachgefuehrt wird

# Das Geraet liefert einzelne Werte als vorzeichenlose 16-Bit-Zahl. 65535
# ist dann keine Leistung, sondern -1. Ab dieser Schwelle wird zurueck-
# gerechnet; alles ueber PLAUSI_MAX gilt danach als ungueltig und wird
# verworfen, damit weder Anzeige noch Energiezaehler verfaelscht werden.
UINT16_SCHWELLE = 32768
PLAUSI_MAX = {
    "143": 5000,    # PV gesamt, Geraet kann max. 4000 W
    "147": 1200, "151": 1200, "164": 1200, "180": 1200,   # je String max 1000 W
    "155": 3000,    # AC-Ausgang
    "128": 3000,    # Batterieleistung
    "137": 3000,    # Netzleistung
    "141": 3000,    # Off-Grid-Last
}
# Nur hier ist ein negativer Wert sinnvoll (er gibt die Richtung an).
# PV-Leistungen koennen nie negativ sein - ein negativer Wert dort ist
# ein Ueberlauf oder Messfehler und wird verworfen.
NEGATIV_ERLAUBT = {"128", "137"}

POLL_FULL = 60             # s zwischen zwei Vollstatus-Abfragen
HEARTBEAT = 9              # s

# Aenderungen einzelner Datenpunkte mitschreiben. Zum Erforschen unbekannter
# DPs: LOG_DPS=1 setzen, dann in der App eine Einstellung aendern und im Log
# nachsehen, welcher Datenpunkt sich bewegt hat.
#   LOG_DPS=0   aus (Standard)
#   LOG_DPS=1   nur bisher unbekannte DPs (uebersichtlich)
#   LOG_DPS=2   alle DPs, auch Messwerte (sehr gespraechig)
LOG_DPS = int(os.getenv("LOG_DPS", "0"))

# Bereits zugeordnete Datenpunkte - nur zur Beschriftung im Log.
DP_NAMES = {
    "102": "SoC", "117": "Modus", "118": "Rueckflussverhinderung",
    "120": "Anti-Rueckstrom", "121": "Einspeisegrenze", "122": "Batterieladegrenze",
    "123": "SoC max", "124": "SoC min", "125": "PV-Ertrag heute", "126": "PV-Ertrag gesamt", "127": "Batt-Spannung",
    "128": "Batt-Leistung", "134": "Status", "137": "Netzleistung",
    "138": "Netzfrequenz", "139": "Netzspannung", "143": "PV gesamt",
    "147": "PV1-P", "148": "PV1-U", "150": "PV1-I", "151": "PV3-P",
    "155": "AC-Ausgang", "156": "PV-Grenze", "163": "PV3-I", "164": "PV2-P",
    "178": "PV2-U", "179": "PV2-I", "180": "PV4-P", "181": "PV4-U",
    "182": "PV4-I", "183": "PV3-U",
    "130": "Zelle max", "131": "Zelle min", "132": "Temperatur 1",
    "133": "Temperatur 2", "140": "Off-Grid-Strom", "141": "Off-Grid-Last",
    "107": "HW Hauptsteuerung", "108": "SW Hauptsteuerung",
    "109": "HW Wechselrichter", "110": "SW Wechselrichter",
    "112": "SW BMS", "113": "HW WLAN", "114": "SW WLAN",
    "184": "SW Wechselrichter PV", "115": "Zaehler-Seriennummer",
    "145": "OTA-URL", "152": "WLAN-Name (Zaehler)",
    "153": "WLAN-Passwort (Zaehler)", "154": "Netzwerkschalter",
    "136": "Netzstrom",
    "142": "Off-Grid-Last (2)", "135": "Off-Grid-Spannung", "119": "Off-Grid-Steckdose",
}

# Messwerte, die sich staendig aendern - im Modus LOG_DPS=1 uninteressant.
DP_NOISY = {"102", "125", "126", "127", "128", "135", "136", "137", "138",
            "139", "142", "143", "147", "148", "150", "151", "155", "163",
            "164", "178", "179", "180", "181", "182", "183"}


def ist_standby(status):
    """Erkennt den Standby-Zustand aus DP 134.

    Die Firmware schreibt "standy_status" (ohne b). Andere Versionen koennten
    "standby" oder aehnliches liefern, deshalb wird nur auf den Wortstamm
    geprueft.
    """
    return "stand" in str(status).lower()


def log_changes(changed, quelle=""):
    if not LOG_DPS or not changed:
        return
    for dp, (alt, neu) in sorted(changed.items(), key=lambda x: int(x[0])):
        if LOG_DPS == 1 and dp in DP_NOISY:
            continue
        name = DP_NAMES.get(dp, "?")
        log.info("DP %-4s %-22s %r -> %r %s", dp, name, alt, neu, quelle)


EVENT_MAX_AGE = 48 * 3600   # s; aeltere Ereignisse fallen raus
EVENT_MAX = 200             # Sicherheitsgrenze fuer die Attributgroesse


def event(text, auch_loggen=True):
    """Haelt ein Ereignis fuer die Anzeige in Home Assistant fest."""
    jetzt = time.time()
    with st.lock:
        st.events.append({
            "ts": int(jetzt),
            "t": datetime.datetime.fromtimestamp(jetzt).strftime("%d.%m. %H:%M:%S"),
            "m": text[:120],
        })
        st.events = [e for e in st.events if jetzt - e["ts"] <= EVENT_MAX_AGE
                     ][-EVENT_MAX:]
        liste = list(st.events)
    if auch_loggen:
        log.info("%s", text)
    try:
        mqttc.publish(f"{BASE}/events", json.dumps({
            "last": text[:250],
            "anzahl": len(liste),
            "eintraege": liste,
        }), retain=True)
    except Exception as exc:
        log.debug("Ereignis konnte nicht veroeffentlicht werden: %s", exc)

logging.basicConfig(
    level=os.getenv("LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ep2500")

# --------------------------------------------------------------------------
# Entitaeten fuer die HA-Discovery
# --------------------------------------------------------------------------
# (dp, key, Anzeigename, Einheit, device_class, state_class, Faktor)

SENSORS = [
    ("102", "soc",           "Ladestand",           "%",   "battery",     "measurement", 1),
    ("155", "ac_out",        "AC-Ausgangsleistung", "W",   "power",       "measurement", 1),
    ("128", "batt_power",    "Batterieleistung",    "W",   "power",       "measurement", 1),
    ("137", "grid_power",    "Netzleistung",        "W",   "power",       "measurement", 1),
    ("143", "pv_power",      "PV-Leistung",         "W",   "power",       "measurement", 1),
    ("125", "pv_energy",     "PV-Ertrag heute",     "Wh",  "energy", "total_increasing", 1),
    ("126", "pv_energy_all", "PV-Ertrag gesamt",    "Wh",  "energy", "total_increasing", 1),
    ("127", "batt_voltage",  "Batteriespannung",    "V",   "voltage",     "measurement", 0.01),
    ("139", "grid_voltage",  "Netzspannung",        "V",   "voltage",     "measurement", 0.1),
    ("138", "grid_freq",     "Netzfrequenz",        "Hz",  "frequency",   "measurement", 0.01),
    ("136", "grid_current",  "Netzstrom",           "A",   "current",     "measurement", 0.1),
    ("134", "status",        "Status",              None,  None,          None,          None),
    ("117", "mode",          "Betriebsmodus",       None,  None,          None,          None),
    # Vier MPPT-Strings. Leistung, Spannung und Strom liegen im Geraet
    # nicht zusammenhaengend, die Zuordnung ist gegen die App verifiziert.
    ("147", "pv1_power",     "PV1-Leistung",        "W",   "power",       "measurement", 1),
    ("148", "pv1_voltage",   "PV1-Spannung",        "V",   "voltage",     "measurement", 0.1),
    ("150", "pv1_current",   "PV1-Strom",           "A",   "current",     "measurement", 0.01),
    ("164", "pv2_power",     "PV2-Leistung",        "W",   "power",       "measurement", 1),
    ("178", "pv2_voltage",   "PV2-Spannung",        "V",   "voltage",     "measurement", 0.1),
    ("179", "pv2_current",   "PV2-Strom",           "A",   "current",     "measurement", 0.01),
    ("151", "pv3_power",     "PV3-Leistung",        "W",   "power",       "measurement", 1),
    ("183", "pv3_voltage",   "PV3-Spannung",        "V",   "voltage",     "measurement", 0.1),
    ("163", "pv3_current",   "PV3-Strom",           "A",   "current",     "measurement", 0.01),
    ("180", "pv4_power",     "PV4-Leistung",        "W",   "power",       "measurement", 1),
    ("181", "pv4_voltage",   "PV4-Spannung",        "V",   "voltage",     "measurement", 0.1),
    ("182", "pv4_current",   "PV4-Strom",           "A",   "current",     "measurement", 0.01),
    # Batterie-Innenwerte (16S2P laut Typenschild)
    ("130", "cell_max",      "Zellspannung max",    "mV",  "voltage",     "measurement", 1),
    ("131", "cell_min",      "Zellspannung min",    "mV",  "voltage",     "measurement", 1),
    ("132", "temp1",         "Temperatur 1",        "°C",  "temperature", "measurement", 0.1),
    ("133", "temp2",         "Temperatur 2",        "°C",  "temperature", "measurement", 0.1),
    ("140", "offgrid_curr",  "Off-Grid-Strom",      "A",   "current",     "measurement", 0.1),
    ("156", "pv_limit",      "PV-Ladegrenze",       "W",   "power",       "measurement", 1),
    # Off-Grid-Steckdose: laeuft separat und ist im AC-Ausgang (155) NICHT
    # enthalten. DP 142 zeigt denselben Wert.
    ("141", "offgrid_power", "Off-Grid-Last",       "W",   "power",       "measurement", 1),
    ("135", "offgrid_volt",  "Off-Grid-Spannung",   "V",   "voltage",     "measurement", 0.1),
]

DEVICE_INFO = {
    "identifiers": [f"ep2500_{DEVICE_ID}"],
    "name": "Oukitel EP2500",
    "manufacturer": "Oukitel",
    "model": "WXY001",
}


# --------------------------------------------------------------------------
# Gemeinsamer Zustand
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.dps = {}              # Cache aller bekannten Datenpunkte
        self.grid = None           # letzter Zaehlerwert in W
        self.grid_ts = 0.0
        self.grid_verdacht = None  # noch unbestaetigter Sprungwert
        self.control_on = False    # Regelung aktiv?
        self.correction = 0        # Zielwert am Zaehler in W
        self.last_write = 0.0
        self.online = False
        self.tune = dict(TUNE_DEFAULTS)
        self.restored = set()      # welche Werte kamen schon aus MQTT zurueck
        self.idle_logged = False   # Leerlauf-Sperre bereits gemeldet?
        self.pass_on = False       # Durchleitung freigegeben?
        self.pass_aktiv = False    # Durchleitung laeuft gerade?
        self.pass_pv_soll = 0      # W; nachgefuehrte PV-Ladeleistung
        self.pass_next = 0.0       # Zeitpunkt des naechsten Nachfuehrschritts
        self.grid_fail = False     # Zaehlerausfall bereits behandelt?
        self.soll = 0.0            # interner Sollwert: >0 einspeisen, <0 laden
        self.events = []           # Ereignisliste fuer das Dashboard
        self.last_status = None    # letzter Geraetestatus (fuer Ereignisse)
        self.last_dir = None       # letzte Richtung: "ein", "laden", "aus"
        self.was_offline = False   # Verbindungsabbruch schon gemeldet?
        self.shelly_ip = SHELLY_IP
        self.shelly_power = None
        self.shelly_on = None
        self.shelly_fails = 0
        self.shelly2_ip = SHELLY2_IP
        self.shelly2_power = None
        self.shelly2_on = None
        self.shelly2_fails = 0

    def tune_get(self, key):
        """Liest einen Regelparameter unter Sperre."""
        with self.lock:
            return self.tune[key]

    def tune_set(self, key, wert):
        with self.lock:
            self.tune[key] = wert

    def merge(self, dps):
        """Uebernimmt Werte in den Cache und liefert die Aenderungen zurueck.

        Unplausible Messwerte werden dabei verworfen: 16-Bit-Ueberlaeufe
        werden in negative Zahlen zurueckgerechnet, Werte ausserhalb des
        physikalisch Moeglichen fliegen ganz raus.
        """
        changed = {}
        with self.lock:
            for k, v in dps.items():
                grenze = PLAUSI_MAX.get(k)
                if grenze is not None and isinstance(v, int):
                    if v >= UINT16_SCHWELLE:
                        v = v - 65536
                    if abs(v) > grenze or (v < 0 and k not in NEGATIV_ERLAUBT):
                        log.warning("DP %s: %s W unplausibel - verworfen", k, v)
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
    """Legt die HA-Entitaeten an. retain=True, damit sie Neustarts ueberleben."""
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

    # Einspeisegrenze als Number
    client.publish(f"{DISC}/number/ep2500/limit/config", json.dumps({
        "name": "Einspeisegrenze",
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

    # Regelung ein/aus
    client.publish(f"{DISC}/switch/ep2500/control/config", json.dumps({
        "name": "Nulleinspeisung-Regelung",
        "unique_id": "ep2500_control",
        "state_topic": f"{BASE}/control/state",
        "command_topic": f"{BASE}/control/set",
        "payload_on": "ON", "payload_off": "OFF",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Korrekturwert: Zielwert am Zaehler statt 0
    client.publish(f"{DISC}/number/ep2500/correction/config", json.dumps({
        "name": "Korrekturwert",
        "unique_id": "ep2500_correction",
        "state_topic": f"{BASE}/correction/state",
        "command_topic": f"{BASE}/correction/set",
        "min": -2000, "max": 2000, "step": 10,
        "unit_of_measurement": "W",
        "mode": "box",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Zaehlerwert, den der Regler tatsaechlich sieht
    client.publish(f"{DISC}/sensor/ep2500/grid/config", json.dumps({
        "name": "Zaehlerleistung (Regler)",
        "unique_id": "ep2500_grid",
        "state_topic": f"{BASE}/grid",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Ladeleistungsgrenze der Batterie (DP 122) - gilt fuer PV UND Netz.
    # Steht der Wert auf 0, nimmt das Geraet gar keinen Solarstrom mehr auf.
    client.publish(f"{DISC}/number/ep2500/charge/config", json.dumps({
        "name": "Batterieladegrenze",
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

    # Rueckflussverhinderung (DP 118). Eingeschaltet sperrt sie die
    # Einspeisung ins Netz - das Geraet geht dann in den Standby.
    client.publish(f"{DISC}/switch/ep2500/backflow/config", json.dumps({
        "name": "Rueckflussverhinderung",
        "icon": "mdi:transmission-tower-export",
        "unique_id": "ep2500_backflow",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.backflow else 'OFF' }}",
        "command_topic": f"{BASE}/backflow/set",
        "payload_on": "ON", "payload_off": "OFF",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Interner Sollwert des Reglers: >0 einspeisen, <0 laden
    client.publish(f"{DISC}/sensor/ep2500/soll/config", json.dumps({
        "name": "Regler-Sollwert",
        "unique_id": "ep2500_soll",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.soll }}",
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Off-Grid-Steckdose am Geraet (DP 119). Weckt den Wechselrichter aus
    # dem Standby, kostet also Eigenverbrauch, wenn nichts angeschlossen ist.
    client.publish(f"{DISC}/switch/ep2500/offgrid/config", json.dumps({
        "name": "Off-Grid-Steckdose",
        "unique_id": "ep2500_offgrid",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.offgrid else 'OFF' }}",
        "command_topic": f"{BASE}/offgrid/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:power-socket-de",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Messender Shelly als unabhaengige Gegenprobe
    client.publish(f"{DISC}/sensor/ep2500/shelly/config", json.dumps({
        "name": "Shelly Leistung",
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

    # Harter Ein/Aus-Schalter. Trennt das Geraet vollstaendig vom Netz -
    # die Bridge verliert dabei die Verbindung.
    client.publish(f"{DISC}/switch/ep2500/shelly/config", json.dumps({
        "name": "Shelly (Netztrennung)",
        "unique_id": "ep2500_shelly_switch",
        "state_topic": f"{BASE}/shelly",
        "value_template": "{{ value_json.state }}",
        "command_topic": f"{BASE}/shelly/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:power-plug",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Zweiter Shelly, z. B. am Nord-Balkonkraftwerk
    client.publish(f"{DISC}/sensor/ep2500/shelly2/config", json.dumps({
        "name": "Nord-BKW Leistung",
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
        "name": "Nord-BKW",
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
        "name": "Nord-BKW Shelly IP",
        "unique_id": "ep2500_shelly2_ip",
        "state_topic": f"{BASE}/shelly2",
        "value_template": "{{ value_json.ip }}",
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

    # SoC-Grenzen des Geraets
    for key, dp, name, icon in (
            ("socmax", DP_SOC_MAX, "Lade-Stopp", "mdi:battery-charging-high"),
            ("socmin", DP_SOC_MIN, "Entlade-Stopp", "mdi:battery-low")):
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

    # Durchleitung: bei vollem Akku PV-Ueberschuss ins Netz statt
    # Nulleinspeisung
    client.publish(f"{DISC}/switch/ep2500/passthrough/config", json.dumps({
        "name": "Durchleitung bei vollem Akku",
        "unique_id": "ep2500_passthrough",
        "state_topic": f"{BASE}/passthrough/state",
        "command_topic": f"{BASE}/passthrough/set",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:transmission-tower-export",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/binary_sensor/ep2500/pass_active/config", json.dumps({
        "name": "Durchleitung laeuft",
        "unique_id": "ep2500_pass_active",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ 'ON' if value_json.passthrough else 'OFF' }}",
        "payload_on": "ON", "payload_off": "OFF",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    client.publish(f"{DISC}/sensor/ep2500/runtime/config", json.dumps({
        "name": "Restlaufzeit",
        "unique_id": "ep2500_runtime",
        "state_topic": f"{BASE}/state",
        "value_template": "{{ value_json.runtime }}",
        "unit_of_measurement": "h",
        "device_class": "duration",
        "icon": "mdi:battery-clock",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Ereignisprotokoll der letzten 48 Stunden
    client.publish(f"{DISC}/sensor/ep2500/events/config", json.dumps({
        "name": "Ereignisse",
        "unique_id": "ep2500_events",
        "state_topic": f"{BASE}/events",
        "value_template": "{{ value_json.last[:250] }}",
        "json_attributes_topic": f"{BASE}/events",
        "json_attributes_template": "{{ {'eintraege': value_json.eintraege, "
                                    "'anzahl': value_json.anzahl} | tojson }}",
        "icon": "mdi:format-list-bulleted",
        "entity_category": "diagnostic",
        "availability_topic": f"{BASE}/available",
        "device": DEVICE_INFO,
    }), retain=True)

    # Regelparameter als Number-Entitaeten
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

    # Abgeloeste Entitaeten entfernen (leere Payload loescht sie in HA)
    for pfad in ("switch/ep2500/gridcharge", "number/ep2500/tune_charge_max",
                 "number/ep2500/tune_hyst", "sensor/ep2500/pv_strings",
                 "number/ep2500/tune_pass_marge"):
        client.publish(f"{DISC}/{pfad}/config", "", retain=True)

    log.info("Discovery veroeffentlicht")


def on_connect(client, userdata, flags, rc, properties=None):
    log.info("MQTT verbunden (rc=%s)", rc)
    publish_discovery(client)
    for t in (f"{BASE}/limit/set", f"{BASE}/control/set", f"{BASE}/charge/set",
              f"{BASE}/backflow/set", f"{BASE}/correction/set",
              f"{BASE}/passthrough/set",
              f"{BASE}/socmax/set", f"{BASE}/socmin/set",
              f"{BASE}/shelly_ip/set", f"{BASE}/shelly/set",
              f"{BASE}/shelly2_ip/set", f"{BASE}/shelly2/set",
              f"{BASE}/offgrid/set"):
        client.subscribe(t)
    if GRID_SOURCE == "mqtt":
        client.subscribe(GRID_TOPIC)
    client.subscribe(f"{BASE}/tune/+/set")

    # Retained States mitlesen, um Einstellungen nach einem Neustart
    # wiederherzustellen, statt sie mit den Defaults zu ueberschreiben.
    client.subscribe(f"{BASE}/tune/+")
    client.subscribe(f"{BASE}/correction/state")
    client.subscribe(f"{BASE}/control/state")
    client.subscribe(f"{BASE}/events")
    client.subscribe(f"{BASE}/passthrough/state")
    client.subscribe(f"{BASE}/shelly_ip")

    # Nach einem MQTT-Reconnect den Verfuegbarkeitsstatus erneut senden,
    # sonst zeigt HA die Entitaeten dauerhaft als nicht verfuegbar an.
    client.publish(f"{BASE}/available",
                   "online" if st.online else "offline", retain=True)

    threading.Timer(3.0, publish_settings).start()


def publish_settings():
    """Veroeffentlicht alles, was nicht aus retained MQTT zurueckkam."""
    if "control" not in st.restored:
        mqttc.publish(f"{BASE}/control/state",
                      "ON" if st.control_on else "OFF", retain=True)
    if "shelly_ip" not in st.restored:
        mqttc.publish(f"{BASE}/shelly_ip", st.shelly_ip, retain=True)
    if "passthrough" not in st.restored:
        mqttc.publish(f"{BASE}/passthrough/state",
                      "ON" if st.pass_on else "OFF", retain=True)
    if "correction" not in st.restored:
        mqttc.publish(f"{BASE}/correction/state", st.correction, retain=True)
    for key, val in st.tune.items():
        if key not in st.restored:
            mqttc.publish(f"{BASE}/tune/{key}", val, retain=True)
    log.info("Regelparameter: %s", st.tune)


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="replace").strip()
    topic = msg.topic

    # Regelparameter setzen: ep2500/tune/<key>/set
    if topic.startswith(f"{BASE}/tune/") and topic.endswith("/set"):
        key = topic.split("/")[-2]
        apply_tune(key, payload, source="HA")
        return

    # Gespeicherten Wert nach Neustart uebernehmen: ep2500/tune/<key>
    if topic.startswith(f"{BASE}/tune/"):
        key = topic.split("/")[-1]
        if key in TUNABLES and key not in st.restored:
            if apply_tune(key, payload, source="gespeichert", echo=False):
                st.restored.add(key)
        return

    if topic == f"{BASE}/control/state":
        if "control" not in st.restored:
            st.control_on = payload.upper() == "ON"
            st.restored.add("control")
            log.info("Regelung %s (gespeichert)",
                     "aktiv" if st.control_on else "aus")
        return

    if topic == f"{BASE}/events":
        if "events" not in st.restored:
            st.restored.add("events")
            try:
                alt = json.loads(payload).get("eintraege", [])
                jetzt = time.time()
                with st.lock:
                    st.events = [e for e in alt
                                 if jetzt - e.get("ts", 0) <= EVENT_MAX_AGE][-EVENT_MAX:]
                log.info("%d frühere Ereignisse uebernommen", len(st.events))
            except Exception:
                pass
        return

    if topic == f"{BASE}/shelly_ip":
        if "shelly_ip" not in st.restored and payload:
            st.shelly_ip = payload
            st.restored.add("shelly_ip")
            log.info("Shelly-Adresse %s (gespeichert)", payload)
        return

    if topic == f"{BASE}/shelly2/set":
        shelly_schalten(payload.upper() == "ON", nr=2)
        return

    if topic == f"{BASE}/shelly2_ip/set":
        st.shelly2_ip = payload.strip()
        log.info("Nord-BKW Shelly-Adresse auf %s geaendert", st.shelly2_ip)
        return

    if topic == f"{BASE}/shelly/set":
        shelly_schalten(payload.upper() == "ON")
        return

    if topic == f"{BASE}/shelly_ip/set":
        st.shelly_ip = payload
        st.shelly_fails = 0
        st.restored.add("shelly_ip")
        client.publish(f"{BASE}/shelly_ip", payload, retain=True)
        event(f"Shelly-Adresse auf {payload} geaendert")
        return

    for key, dp, name in (("socmax", DP_SOC_MAX, "Lade-Stopp"),
                          ("socmin", DP_SOC_MIN, "Entlade-Stopp")):
        if topic == f"{BASE}/{key}/set":
            val = parse_number(payload)
            if val is not None:
                val = max(0, min(100, int(val)))
                with _dev_lock:
                    if _dev is not None:
                        try:
                            res = _dev.set_value(int(dp), val)
                            ok, grund = antwort_ok(res)
                            if ok:
                                st.merge({dp: val})
                                event(f"{name} auf {val} % gesetzt")
                            else:
                                log.warning("%s abgelehnt: %s", name, grund)
                        except Exception as exc:
                            log.error("%s setzen fehlgeschlagen: %s", name, exc)
                publish_state()
            return

    if topic == f"{BASE}/passthrough/state":
        if "passthrough" not in st.restored:
            st.pass_on = payload.upper() == "ON"
            st.restored.add("passthrough")
            log.info("Durchleitung %s (gespeichert)",
                     "freigegeben" if st.pass_on else "gesperrt")
        return

    if topic == f"{BASE}/passthrough/set":
        st.pass_on = payload.upper() == "ON"
        st.restored.add("passthrough")
        client.publish(f"{BASE}/passthrough/state",
                       "ON" if st.pass_on else "OFF", retain=True)
        event("Durchleitung " + ("freigegeben" if st.pass_on else "gesperrt"))
        return

    if topic == f"{BASE}/backflow/set":
        sperren = payload.upper() == "ON"
        set_backflow(sperren, quelle="HA")
        event("Rueckflussverhinderung "
              + ("eingeschaltet - Einspeisung gesperrt" if sperren
                 else "ausgeschaltet - Einspeisung moeglich"))
        publish_state()
        return

    if topic == f"{BASE}/offgrid/set":
        an = payload.upper() == "ON"
        with _dev_lock:
            if _dev is not None:
                try:
                    res = _dev.set_value(int(DP_OFFGRID), an)
                    ok, grund = antwort_ok(res)
                    if ok:
                        st.merge({DP_OFFGRID: an})
                    else:
                        log.warning("Off-Grid-Steckdose abgelehnt: %s", grund)
                except Exception as exc:
                    log.error("Off-Grid-Steckdose schalten fehlgeschlagen: %s", exc)
        event("Off-Grid-Steckdose " + ("eingeschaltet" if an else "ausgeschaltet"))
        publish_state()
        return

    if topic == f"{BASE}/charge/set":
        val = parse_number(payload)
        if val is not None:
            # Auch den gespeicherten Sollwert mitziehen, sonst setzt der
            # Waechter die Grenze binnen 30 s wieder zurueck.
            val = max(0, min(CHARGE_HW_MAX, int(val)))
            st.tune_set("charge_limit", val)
            mqttc.publish(f"{BASE}/tune/charge_limit", val, retain=True)
            set_charge(val, reason="manuell")
        return

    if topic == f"{BASE}/correction/state":
        if "correction" not in st.restored:
            val = parse_number(payload)
            if val is not None:
                st.correction = int(val)
                st.restored.add("correction")
        return

    if topic == GRID_TOPIC:
        if GRID_SOURCE != "mqtt":
            return          # HTTP-Quelle aktiv, MQTT darf sie nicht ueberschreiben
        val = parse_number(payload, GRID_JSON_KEY)
        if val is None:
            return
        # Dieselbe Pruefung und Sprungerkennung wie im HTTP-Pfad. Die
        # Funktion veroeffentlicht den Wert auch selbst, sonst bliebe der
        # Sensor "Zaehlerleistung (Regler)" in HA leer.
        zaehlerwert_uebernehmen(val, "MQTT")
        return

    if topic == f"{BASE}/limit/set":
        val = parse_number(payload)
        if val is not None:
            set_limit(int(val), reason="manuell")
        return

    if topic == f"{BASE}/control/set":
        st.control_on = payload.upper() == "ON"
        st.restored.add("control")
        client.publish(f"{BASE}/control/state",
                       "ON" if st.control_on else "OFF", retain=True)
        event("Regelung " + ("eingeschaltet" if st.control_on else "ausgeschaltet"))
        return

    if topic == f"{BASE}/correction/set":
        val = parse_number(payload)
        if val is not None:
            st.correction = int(val)
            st.restored.add("correction")
            client.publish(f"{BASE}/correction/state", st.correction, retain=True)
            log.info("Korrekturwert = %s W", st.correction)


def apply_tune(key, payload, source="", echo=True):
    """Uebernimmt einen Regelparameter, begrenzt auf den erlaubten Bereich."""
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
    """Akzeptiert '812', '812.0' oder {'power': 812}.

    NaN und Infinity werden abgewiesen - ein spaeteres int() wuerde sonst
    eine Exception werfen und den Regelschritt abbrechen.
    """
    try:
        wert = float(payload)
        return wert if math.isfinite(wert) else None
    except ValueError:
        pass
    try:
        data = json.loads(payload)
        if json_key and isinstance(data, dict) and json_key in data:
            wert = float(data[json_key])
            return wert if math.isfinite(wert) else None
    except Exception:
        pass
    log.warning("Unlesbarer Payload: %r", payload[:80])
    return None


mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ep2500-bridge")
if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
mqttc.will_set(f"{BASE}/available", "offline", retain=True)
mqttc.on_connect = on_connect
mqttc.on_message = on_message


# --------------------------------------------------------------------------
# Tuya
# --------------------------------------------------------------------------

_dev_lock = threading.Lock()
_dev = None


def connect_device():
    global _dev
    d = tinytuya.OutletDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY, port=DEVICE_PORT)
    d.set_version(3.5)
    d.set_socketPersistent(True)
    d.set_socketTimeout(8)
    _dev = d
    return d


def antwort_ok(res):
    """Prueft die Antwort von TinyTuya auf einen Schreibvorgang.

    TinyTuya wirft bei Fehlern keine Exception, sondern liefert ein Dict mit
    einem "Error"-Schluessel zurueck. Ohne diese Pruefung wuerde ein
    fehlgeschlagener Befehl als erfolgreich gelten und der angeforderte Wert
    im Cache landen - Home Assistant zeigt dann etwas an, das im Geraet nie
    angekommen ist.
    """
    if res is None:
        return False, "keine Antwort"
    if isinstance(res, dict) and "Error" in res:
        return False, f"{res.get('Error')} (Err {res.get('Err')})"
    return True, ""


def write_dp(dp, watt, vmax, reason=""):
    """Schreibt einen Leistungs-Datenpunkt, hart auf 0..vmax begrenzt."""
    watt = max(0, min(vmax, int(watt)))
    name = {DP_LIMIT: "Einspeisegrenze", DP_CHARGE: "Batterieladegrenze",
            DP_PV_LIMIT: "PV-Grenze"}.get(dp, f"DP {dp}")
    with _dev_lock:
        if _dev is None:
            return False
        try:
            res = _dev.set_value(int(dp), watt)
        except Exception as exc:
            log.error("%s schreiben fehlgeschlagen: %s", name, exc)
            return False

    ok, grund = antwort_ok(res)
    if not ok:
        log.warning("%s -> %s W abgelehnt: %s", name, watt, grund)
        return False

    st.last_write = time.time()
    st.merge({dp: watt})
    log.info("%s -> %s W (%s)", name, watt, reason)
    return True


def set_limit(watt, reason=""):
    return write_dp(DP_LIMIT, watt, LIMIT_MAX, reason)


def set_charge(watt, reason=""):
    return write_dp(DP_CHARGE, watt, CHARGE_HW_MAX, reason)


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
    out["soll"] = int(round(st.soll))
    out["passthrough"] = st.pass_aktiv
    if DP_OFFGRID in dps:
        out["offgrid"] = bool(dps[DP_OFFGRID])
    if DP_BACKFLOW in dps:
        out["backflow"] = bool(dps[DP_BACKFLOW])
    if DP_SOC_MAX in dps:
        out["socmax"] = dps[DP_SOC_MAX]
    if DP_SOC_MIN in dps:
        out["socmin"] = dps[DP_SOC_MIN]

    # Restlaufzeit: nutzbare Energie oberhalb des Entlade-Stopp-SoC geteilt
    # durch die aktuelle Entladeleistung. Nur sinnvoll, solange entladen
    # wird - beim Laden oder im Standby bleibt der Wert leer.
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
    """Haelt die Verbindung, mergt Delta-Frames, pollt periodisch voll."""
    while True:
        try:
            d = connect_device()
            data = d.status()
            if not (isinstance(data, dict) and "dps" in data):
                raise RuntimeError(f"Status fehlgeschlagen: {data}")
            # Nach einem Reconnect zeigt der Vergleich mit dem Cache, was sich
            # zwischenzeitlich geaendert hat - etwa durch die Oukitel-App.
            log_changes(st.merge(data["dps"]), "(Vollstatus)")
            st.online = True
            mqttc.publish(f"{BASE}/available", "online", retain=True)
            publish_state()
            log.info("Verbunden, %d Datenpunkte", len(data["dps"]))
            if st.was_offline:
                event("Verbindung zum Geraet wiederhergestellt")
                st.was_offline = False

            next_hb = time.time() + HEARTBEAT
            next_full = time.time() + POLL_FULL

            while True:
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
                    with _dev_lock:
                        full = d.status()
                    if isinstance(full, dict) and "dps" in full:
                        log_changes(st.merge(full["dps"]), "(Poll)")
                        publish_state()
                    next_full = now + POLL_FULL

        except Exception as exc:
            st.online = False
            mqttc.publish(f"{BASE}/available", "offline", retain=True)
            if not st.was_offline:
                event(f"Verbindung zum Geraet verloren ({exc})", auch_loggen=False)
                st.was_offline = True
            log.warning("Verbindung verloren (%s), neuer Versuch in 15 s", exc)
            time.sleep(15)


# --------------------------------------------------------------------------
# Eco Tracker (everHome Local API)
# --------------------------------------------------------------------------

def zaehlerwert_uebernehmen(val, quelle=""):
    """Prueft einen Zaehlerwert und uebernimmt ihn in den Zustand.

    Wird von beiden Quellen benutzt (HTTP-Abfrage und MQTT-Topic), damit der
    Regler in beiden Faellen dieselbe Filterung sieht.

    Zwei Stufen:
      1. Offensichtlicher Unsinn (nicht endlich, jenseits ECO_ABSURD) fliegt
         sofort raus.
      2. Ein Sprung groesser als ECO_SPIKE gegenueber dem letzten gueltigen
         Wert wird erst uebernommen, wenn die naechste Messung ihn
         bestaetigt. Ein einzelner Ausreisser wuerde den Regler sonst auf
         Anschlag treiben.

    Rueckgabe: True, wenn der Wert uebernommen wurde.
    """
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return False
    val = float(val)

    if not math.isfinite(val) or abs(val) > ECO_ABSURD:
        log.warning("Zaehlerwert %.0f W unplausibel - verworfen%s", val,
                    f" ({quelle})" if quelle else "")
        return False

    meldung = None      # Ereignis, nach dem Lock abzusetzen
    sprung = None       # gesetzt, wenn auf Bestaetigung gewartet wird
    with st.lock:
        letzter = st.grid
        verdacht = st.grid_verdacht
        if (ECO_SPIKE and letzter is not None
                and abs(val - letzter) > ECO_SPIKE):
            if verdacht is None or abs(val - verdacht) > ECO_SPIKE:
                st.grid_verdacht = val
                sprung = letzter
            else:
                meldung = (f"Zaehlersprung bestaetigt: {letzter:+.0f} -> "
                           f"{val:+.0f} W")
        elif verdacht is not None:
            meldung = (f"Ausreisser verworfen: {verdacht:+.0f} W "
                       f"(Zaehler blieb bei {val:+.0f} W)")
        if sprung is None:
            st.grid_verdacht = None
            st.grid = val
            st.grid_ts = time.time()

    # event() nimmt selbst st.lock - deshalb erst hier, nicht im Block oben.
    if sprung is not None:
        log.info("Zaehlersprung %.0f -> %.0f W - warte auf Bestaetigung",
                 sprung, val)
        return False
    if meldung:
        event(meldung, auch_loggen=False)
    mqttc.publish(f"{BASE}/grid", int(val))
    return True


def eco_loop():
    """Fragt den Eco Tracker direkt per HTTP ab - unabhaengig von HA."""
    fails = 0
    while True:
        try:
            with urllib.request.urlopen(ECO_URL, timeout=5) as resp:
                data = json.loads(resp.read().decode())

            val = data.get(ECO_FIELD)
            if val is None:
                raise ValueError(f"Feld {ECO_FIELD!r} fehlt in der Antwort")

            # agePower ist das Alter des Messwerts in ms. Ein sehr alter Wert
            # bedeutet, dass der Tracker selbst keine Daten mehr bekommt.
            age = data.get("agePower")
            if isinstance(age, (int, float)) and age > 30000:
                log.warning("Eco Tracker liefert veraltete Daten (%.0f ms)", age)
                fails += 1
                time.sleep(ECO_INTERVAL)
                continue

            val = float(val)

            if not zaehlerwert_uebernehmen(val, "HTTP"):
                time.sleep(ECO_INTERVAL)
                continue

            if fails >= 5:
                event("Eco Tracker wieder erreichbar", auch_loggen=False)
            fails = 0

        except Exception as exc:
            fails += 1
            if fails in (1, 5) or fails % 20 == 0:
                log.warning("Eco Tracker nicht erreichbar (%dx): %s", fails, exc)
                if fails == 5:
                    event("Eco Tracker antwortet nicht", auch_loggen=False)

        time.sleep(ECO_INTERVAL)


# --------------------------------------------------------------------------
# Regelkreis
# --------------------------------------------------------------------------

def shelly_rpc(ip, pfad):
    """Ruft die RPC-Schnittstelle eines Shelly auf (Gen2/Gen3)."""
    with urllib.request.urlopen(f"http://{ip}/rpc/{pfad}", timeout=4) as resp:
        return json.loads(resp.read().decode())


def shelly_status(ip):
    """Liest Leistung und Schaltzustand. Gen2/Gen3 zuerst, dann Gen1."""
    try:
        d = shelly_rpc(ip, "Switch.GetStatus?id=0")
        return d.get("apower"), d.get("output")
    except Exception:
        pass
    # Gen1: /status liefert meters[0].power und relays[0].ison
    with urllib.request.urlopen(f"http://{ip}/status", timeout=4) as resp:
        d = json.loads(resp.read().decode())
    leistung = (d.get("meters") or [{}])[0].get("power")
    an = (d.get("relays") or [{}])[0].get("ison")
    return leistung, an


def shelly_schalten_roh(ip, an):
    """Schaltet einen Shelly. Gen2/Gen3 zuerst, dann Gen1."""
    try:
        shelly_rpc(ip, f"Switch.Set?id=0&on={'true' if an else 'false'}")
        return
    except Exception:
        pass
    url = f"http://{ip}/relay/0?turn={'on' if an else 'off'}"
    with urllib.request.urlopen(url, timeout=4):
        pass


def shelly_abfragen(nr):
    """Liest Leistung und Schaltzustand eines der beiden Shellys."""
    ip = st.shelly_ip if nr == 1 else st.shelly2_ip
    name = "Shelly" if nr == 1 else "Shelly Nord-BKW"
    topic = f"{BASE}/shelly" if nr == 1 else f"{BASE}/shelly2"
    if not ip:
        return
    try:
        leistung, an = shelly_status(ip)
        with st.lock:
            if nr == 1:
                st.shelly_power = float(leistung) if leistung is not None else None
                st.shelly_on = bool(an) if an is not None else None
            else:
                st.shelly2_power = float(leistung) if leistung is not None else None
                st.shelly2_on = bool(an) if an is not None else None
        fails = st.shelly_fails if nr == 1 else st.shelly2_fails
        if fails >= 5:
            event(f"{name} wieder erreichbar ({ip})", auch_loggen=False)
        if nr == 1:
            st.shelly_fails = 0
        else:
            st.shelly2_fails = 0
        mqttc.publish(topic, json.dumps({
            "power": round(leistung) if leistung is not None else None,
            "state": "ON" if an else "OFF",
            "ip": ip,
        }))
    except Exception as exc:
        if nr == 1:
            st.shelly_fails += 1
            fails = st.shelly_fails
        else:
            st.shelly2_fails += 1
            fails = st.shelly2_fails
        if fails in (1, 5) or fails % 60 == 0:
            log.warning("%s %s nicht erreichbar (%dx): %s", name, ip, fails, exc)
        if fails == 5:
            event(f"{name} {ip} antwortet nicht", auch_loggen=False)


def shelly_loop():
    while True:
        shelly_abfragen(1)
        shelly_abfragen(2)
        time.sleep(SHELLY_INTERVAL)


def shelly_schalten(an, nr=1):
    """Schaltet einen Shelly. Trennt das jeweilige Geraet hart vom Netz."""
    ip = st.shelly_ip if nr == 1 else st.shelly2_ip
    name = "Shelly" if nr == 1 else "Shelly Nord-BKW"
    try:
        shelly_schalten_roh(ip, an)
        with st.lock:
            if nr == 1:
                st.shelly_on = bool(an)
            else:
                st.shelly2_on = bool(an)
        event(f"{name} {'eingeschaltet' if an else 'ausgeschaltet'} ({ip})")
        return True
    except Exception as exc:
        log.error("%s schalten fehlgeschlagen: %s", name, exc)
        return False


def charge_guard_loop():
    """Haelt die Batterieladegrenze (DP 122) auf dem eingestellten Wert.

    DP 122 begrenzt die Ladeleistung der Batterie insgesamt - aus PV *und*
    aus dem Netz. Steht der Wert auf 0, schaltet das Geraet die MPPT-Regler
    ab und nimmt gar keinen Solarstrom mehr auf, auch bei leerem Akku und
    voller Sonne. Der Wert muss deshalb dauerhaft oben stehen.

    DP 118 (Rueckflussverhinderung) sperrt uebrigens die Einspeisung ins
    Netz, nicht das Laden aus dem Netz - der Name ist irrefuehrend.
    """
    while True:
        time.sleep(30)
        if not st.online:
            continue
        soll = st.tune["charge_limit"]
        ist = st.snapshot().get(DP_CHARGE)
        if isinstance(ist, int) and ist != soll:
            log.info("Batterieladegrenze steht auf %s W statt %s W - korrigiert",
                     ist, soll)
            set_charge(soll, reason="Freigabe PV-Ladung")


def set_pv_limit(watt, reason=""):
    """Schreibt DP 156, die PV-Ladeleistung.

    Damit laesst sich die Solarseite drosseln, ohne die Einspeisung
    anzutasten. Achtung: Das Geraet fuehrt diesen Wert auch selbst nach,
    wenn in der App ein PV-Zeitplan hinterlegt ist.
    """
    return write_dp(DP_PV_LIMIT, watt, 4000, reason)


def set_backflow(sperren, quelle=""):
    """Schaltet die Rueckflussverhinderung (DP 118).

    Der Name meint den Rueckfluss *ins Netz*: Eingeschaltet sperrt sie die
    Einspeisung vollstaendig, das Geraet geht in den Standby. Auf das Laden
    aus dem Netz hat sie keinen Einfluss.
    """
    with _dev_lock:
        if _dev is None:
            return False
        try:
            res = _dev.set_value(int(DP_BACKFLOW), bool(sperren))
        except Exception as exc:
            log.error("Rueckflussverhinderung schalten fehlgeschlagen: %s", exc)
            return False
    ok, grund = antwort_ok(res)
    if not ok:
        log.warning("Rueckflussverhinderung abgelehnt: %s", grund)
        return False
    st.merge({DP_BACKFLOW: bool(sperren)})
    log.info("Einspeisung %s%s", "gesperrt" if sperren else "freigegeben",
             f" ({quelle})" if quelle else "")
    return True


def control_loop():
    """Regelt die Einspeisung ueber DP 121.

    Nur eine Richtung: Ist am Zaehler Bezug vorhanden, gibt das Geraet mehr
    ab; ist Ueberschuss da, wird die Einspeisegrenze zurueckgenommen und die
    PV laedt automatisch den Akku. Ein Stellglied fuer die Ladeleistung
    braucht es dafuer nicht - DP 122 bleibt dauerhaft offen, siehe
    charge_guard_loop().

    Oberhalb von "Durchleitung ab SoC" uebernimmt ein zweiter, voellig
    anderer Zweig: dort wird nicht mehr auf den Zaehler geregelt, sondern der
    Ladestand gehalten und konstant eingespeist. Siehe die Kommentare dort.
    """
    while True:
        time.sleep(st.tune["interval"])

        if not st.control_on or not st.online:
            continue

        with st.lock:
            grid = st.grid
            grid_age = time.time() - st.grid_ts
            target = st.correction

        if grid is None or grid_age > GRID_MAX_AGE:
            if st.pass_aktiv:
                # Die Durchleitung regelt auf den Ladestand, nicht auf den
                # Zaehler - sie laeuft ohne Zaehlerwert unveraendert weiter.
                # Sie hier abzubrechen wuerde die PV-Grenze ganz oeffnen und
                # den Akku bei voller Sonne in die Abschaltung bei 100 %
                # treiben. Genau davor soll die Durchleitung schuetzen.
                if not st.grid_fail:
                    st.grid_fail = True
                    event("Zaehler liefert keine Werte - Durchleitung laeuft "
                          "unveraendert weiter")
                continue
            if not st.grid_fail:
                st.grid_fail = True
                event(f"Zaehler liefert keine Werte - Einspeisegrenze auf "
                      f"{GRID_FAIL_LIMIT} W")
                # Ohne Zaehlerwert kann nicht geregelt werden. Die zuletzt
                # gesetzte Grenze einfach stehen zu lassen hiesse, blind
                # weiterzuspeisen.
                set_limit(GRID_FAIL_LIMIT, reason="Zaehlerausfall")
                st.soll = float(GRID_FAIL_LIMIT)
                publish_state()
            continue
        if st.grid_fail:
            st.grid_fail = False
            event("Zaehler liefert wieder Werte - Regelung aktiv")

        dps = st.snapshot()
        limit = dps.get(DP_LIMIT)
        ac_out = dps.get(DP_AC_OUT)
        status = dps.get("134")
        if limit is None:
            continue

        ist = float(ac_out) if isinstance(ac_out, (int, float)) else 0.0
        fehler = grid - target          # >0 = Netzbezug, Geraet muss mehr abgeben

        # Leerlauf-Sperre: Nur wenn tatsaechlich Leistung angefordert wurde
        # und trotzdem nichts kommt (leerer Akku). Steht die Grenze auf 0,
        # weil der Regler sie selbst dorthin gesetzt hat, darf nicht
        # pausiert werden - sonst faehrt er nie wieder an.
        leerlauf = (ist_standby(status) and ist == 0.0 and limit >= 20
                    and fehler > 0)
        if leerlauf:
            if not st.idle_logged:
                event("Regelung pausiert - Geraet im Standby, "
                      f"Akku {dps.get('102', '?')} %")
                st.idle_logged = True
            continue
        if st.idle_logged:
            event("Regelung wieder aktiv - Geraet reagiert")
            st.idle_logged = False

        # ------------------------------------------------------------------
        # Durchleitung
        # ------------------------------------------------------------------
        # Ziel: Der Ladestand bleibt stehen und das Geraet speist konstant
        # LIMIT_MAX ins Hausnetz. Weil PV beim EP2500 immer ueber den Akku
        # laeuft, heisst das schlicht: PV-Ladeleistung so einstellen, dass
        # sie die Einspeisung deckt. Zwei feste Werte, keine Bilanzregelung.
        #
        # Nachgefuehrt wird nur der Wandlungsverlust, und zwar am Ladestand.
        # Der Verlust wirkt in die sichere Richtung: 800 W in den Akku ergeben
        # am Ausgang weniger als 800 W, der Ladestand sinkt also von selbst
        # leicht ab. Ueberschreitungen sind damit die Ausnahme, nicht die
        # Regel - deshalb genuegt ein kleiner Schritt alle paar Minuten.
        soc = dps.get("102")
        ziel = st.tune["soc_pass"]
        if st.pass_on and isinstance(soc, (int, float)):
            # Hysterese: einmal aktiv, bleibt es bis PASS_HYST unter dem Ziel
            grenze = ziel - PASS_HYST if st.pass_aktiv else ziel
            aktiv = soc >= grenze
        else:
            aktiv = False

        if aktiv != st.pass_aktiv:
            st.pass_aktiv = aktiv
            if aktiv:
                st.pass_pv_soll = LIMIT_MAX
                st.pass_next = time.time() + PASS_SETTLE
                event(f"Durchleitung aktiv - halte Ladestand bei {ziel} %, "
                      f"{LIMIT_MAX} W ins Hausnetz")
                set_limit(LIMIT_MAX, reason="Durchleitung")
                set_pv_limit(st.pass_pv_soll, reason="Durchleitung")
                st.soll = float(LIMIT_MAX)
            else:
                event("Durchleitung beendet - zurueck auf Nulleinspeisung")
                # PV-Grenze wieder ganz oeffnen, sonst bleibt die Ernte
                # gedrosselt
                set_pv_limit(st.tune["pv_max"], reason="Durchleitung beendet")
            publish_state()

        if aktiv:
            # Die Einspeisegrenze steht fest. Falls sie jemand anders
            # verstellt hat (App, abgelehntes Kommando), zurueckholen.
            if isinstance(limit, int) and limit != LIMIT_MAX:
                log.info("Durchleitung | Einspeisegrenze steht auf %s W "
                         "statt %s W - korrigiert", limit, LIMIT_MAX)
                set_limit(LIMIT_MAX, reason="Durchleitung: Grenze halten")
                st.soll = float(LIMIT_MAX)
                publish_state()

            if time.time() < st.pass_next:
                continue

            abweichung = int(soc) - ziel
            if abweichung == 0:
                # Ladestand sitzt auf dem Ziel - nichts tun.
                st.pass_next = time.time() + PASS_ADJUST
                continue

            # Schrittweite proportional zur Abweichung. Wichtig ist die
            # Symmetrie: runter und rauf gleich schnell. Ein schnelles
            # Absenken mit langsamem Zurueckholen wuerde den Ladestand bei
            # jeder Wolke weiter nach unten treiben.
            schritt = max(-PASS_STEP_FAST,
                          min(PASS_STEP_FAST, -abweichung * PASS_STEP))
            wartezeit = (PASS_ADJUST_FAST if abs(abweichung) >= PASS_ALARM
                         else PASS_ADJUST)
            st.pass_next = time.time() + wartezeit

            # Anti-Windup: Nach oben nur nachfuehren, wenn die Grenze
            # ueberhaupt wirkt. Liefert die PV weniger als erlaubt, ist die
            # Wolke die Ursache und nicht die Einstellung - den Sollwert dann
            # weiter hochzudrehen aendert nichts, und bei der naechsten
            # Aufklarung schiesst der Ladestand ueber.
            pv_ist = dps.get("143")
            if (schritt > 0 and isinstance(pv_ist, (int, float))
                    and pv_ist < st.pass_pv_soll - PASS_STEP):
                continue

            neu = max(0, min(st.tune["pv_max"], st.pass_pv_soll + schritt))
            if neu == st.pass_pv_soll:
                continue
            st.pass_pv_soll = neu
            log.info("Durchleitung | SoC %s %% (Ziel %s) | Akku %s W | "
                     "PV-Ladeleistung -> %s W",
                     soc, ziel, dps.get(DP_BATT, "?"), neu)
            set_pv_limit(neu, reason="Durchleitung: Ladestand halten")
            publish_state()
            continue

        if abs(fehler) <= st.tune["deadband"]:
            continue

        # Anti-Windup: Der Sollwert darf sich nicht beliebig weit von dem
        # entfernen, was das Geraet tatsaechlich schafft.
        soll = max(ist - WINDUP_MARGIN, min(ist + WINDUP_MARGIN, st.soll))

        max_step = st.tune["max_step"]
        soll += max(-max_step, min(max_step, fehler * st.tune["gain"]))
        soll = max(0.0, min(float(LIMIT_MAX), soll))
        st.soll = soll

        neu = int(round(soll))
        if abs(neu - limit) < CTRL_MIN_STEP:
            continue

        log.info("Zaehler %+.0f W (Ziel %+d) | Ist %.0f W | Einspeisen %s -> %s",
                 grid, target, ist, limit, neu)
        set_limit(neu, reason="Regelung")
        publish_state()


def main():
    fehlend = [n for n, v in (("EP2500_ID", DEVICE_ID),
                              ("EP2500_IP", DEVICE_IP),
                              ("EP2500_KEY", LOCAL_KEY)) if not v]
    if fehlend:
        log.error("Bitte %s in der Umgebung setzen (siehe README).",
                  ", ".join(fehlend))
        raise SystemExit(1)

    # connect_async blockiert nicht: die Bridge startet auch dann, wenn der
    # Broker noch nicht erreichbar ist, und verbindet sich spaeter selbst.
    mqttc.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqttc.loop_start()

    threading.Thread(target=tuya_loop, daemon=True).start()
    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=charge_guard_loop, daemon=True).start()
    threading.Thread(target=shelly_loop, daemon=True).start()

    if GRID_SOURCE == "http":
        log.info("Zaehlerquelle: HTTP %s (Feld %s)", ECO_URL, ECO_FIELD)
        threading.Thread(target=eco_loop, daemon=True).start()
    else:
        log.info("Zaehlerquelle: MQTT %s", GRID_TOPIC)

    beenden = threading.Event()

    def stoppen(signum, frame):
        log.info("Signal %s empfangen", signum)
        beenden.set()

    # docker stop schickt SIGTERM - ohne Behandlung liefe das Aufraeumen nie.
    signal.signal(signal.SIGTERM, stoppen)
    signal.signal(signal.SIGINT, stoppen)

    beenden.wait()

    log.info("Beende, setze Einspeisegrenze auf %s W", LIMIT_SAFE)
    if st.pass_aktiv:
        # Eine von der Durchleitung gesetzte PV-Drosselung wieder oeffnen
        set_pv_limit(st.tune["pv_max"], reason="Shutdown")
    set_limit(LIMIT_SAFE, reason="Shutdown")
    mqttc.publish(f"{BASE}/available", "offline", retain=True)
    time.sleep(1)
    mqttc.loop_stop()


if __name__ == "__main__":
    main()
