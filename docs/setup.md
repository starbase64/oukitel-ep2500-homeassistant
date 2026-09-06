# Setup guide

From a Tuya developer account to a running zero-export controller.

For the datapoint reference see [datapoints.md](datapoints.md).

*A note on the app labels: my app runs in German, so the English labels quoted
here are my translations. If yours differ slightly, go by the position in the
menu.*

---

# Part 1: getting the device ID and local key

The local connection needs two values that are only available through a Tuya
developer account. It is free.

## 1.1 Create an account

Register at **iot.tuya.com**. A personal account is enough, no company needed.

## 1.2 Create a cloud project

Under **Cloud → Development → Create Cloud Project**:

- **Industry:** Smart Home
- **Development Method:** Smart Home
- **Data Center:** pick the region your app account is registered in – for
  Europe that is **Central Europe**. This is the most common mistake. With the
  wrong region the wizard will not find your devices later.

After creating it you get an **Access ID** (client ID) and **Access Secret**
(client secret). Note both down.

## 1.3 Check API permissions

Under **Service API** in your project, at least these need to be enabled:

- IoT Core
- Authorization Token Management
- Smart Home Scene Linkage

If something is missing, add it via **Go to Authorize**. IoT Core is
time-limited and needs occasional renewal – one click in the portal.

## 1.4 Link your app account

In the project go to **Devices → Link App Account → Add App Account**. A QR
code appears.

Scan it in the Smart Life or Oukitel app: Profile → scan icon, top right. Your
devices then show up in the portal.

## 1.5 Read the local key

Now TinyTuya comes in. On any machine:

```
pip install tinytuya
python -m tinytuya wizard
```

The wizard asks for the access ID, access secret, the device ID of any one
device (found in the portal under Devices) and the region. Afterwards you have
a `devices.json` containing all devices including their **local key**.

The EP2500 appears there as category `qt`, product "Balcony Energy Storage",
model `WXY001` – and **without** a `mapping` block. That is normal, and at the
same time the reason no ready-made integration exists: Oukitel has not
registered a data model in the cloud. Querying the device functions returns an
empty list.

> **Important:** the local key changes whenever you re-pair the device in the
> app. If you hit connection problems later, that is the first suspect – just
> run the wizard again.

## 1.6 Find the IP address

Most reliably from your router's DHCP list. Look for the device MAC or a
hostname containing "ESP" or "Tuya".

`python -m tinytuya scan` also works, but relies on UDP broadcasts – and those
do not cross router boundaries. If the EP2500 sits in a different network
segment, the scan finds nothing even though a direct connection works fine.

On Windows there is more: the firewall blocks the broadcasts, and without the
`psutil` module TinyTuya listens on only one network interface.

---

# Part 2: testing the connection

The EP2500 speaks **protocol 3.5**, not the more common 3.3. With the wrong
version you get:

- `Err 914 – Check device key or version`
- `Err 904 – Unexpected Payload from Device`

Both usually mean "wrong version", not "wrong key".

This script tries them all. Save it as `ep2500_probe.py`:

```python
#!/usr/bin/env python3
import json, tinytuya

DEVICE_ID = "YOUR_DEVICE_ID"
DEVICE_IP = "YOUR_DEVICE_IP"
LOCAL_KEY = "YOUR_LOCAL_KEY"

for v in (3.3, 3.4, 3.5, 3.2, 3.1):
    d = tinytuya.OutletDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY, port=6668)
    d.set_version(v)
    d.set_socketTimeout(8)
    r = d.status()
    ok = isinstance(r, dict) and "dps" in r
    print(f"--- version {v}: {'HIT' if ok else 'no'}")
    if ok:
        print(json.dumps(r, indent=2))
        break
```

Run it with `python ep2500_probe.py`.

> **The app must be closed**, including in the background on your phone. Tuya
> devices accept only **one** local connection at a time. This applies in
> normal operation too: opening the app kicks out your integration.

On success you will see around 83 datapoints.

---

# Part 4: DP 122 – where I lost two days

**Read this before you start troubleshooting anything.**

In the app DP 122 is labelled "maximum permitted battery charge power". What
the name does not tell you: it limits the **total** charge power – from PV
*and* from the grid.

Set it to 0 and the device shuts down its MPPT controllers and takes **no solar
energy at all**. Even with an empty battery and full sun. Open-circuit voltage
is present at the inputs (around 37 V) but no current flows (0.01 A).

Mine was set to 0, and the solar harvest appeared to stop arbitrarily. The
device went into standby because it had nothing to do – and in standby it does
not harvest. At one point I was convinced the unit was faulty.

**Consequence for any integration:** DP 122 must be held permanently above your
PV power. For a 2200 Wp array that means around 2500 W. It is not a control
variable, it is an enable.

**Careful with DP 118 (backflow prevention):** the name refers to backflow
*into the grid*. Enabling it blocks **export** entirely – the device goes to
standby. It has no effect on charging from the grid. I had it the other way
round at first.

I have not found a reliable switch that blocks only grid charging while
allowing PV charging.

---

# Part 5: setting up the bridge

I built the integration as a standalone Python service running next to HA in
Docker. It holds the Tuya connection, publishes everything via MQTT discovery
and runs the control loop. The script is `ep2500_bridge.py` in the repository root.

## 5.1 Create the directory

```bash
git clone https://github.com/YOUR_NAME/oukitel-ep2500-homeassistant.git
cd oukitel-ep2500-homeassistant
cp docker-compose.example.yml docker-compose.yml
```

Then edit `docker-compose.yml` with your values.

## 5.2 Configuration

The `Dockerfile` is already in the repository. Adjust `docker-compose.yml`

```yaml
services:
  ep2500-bridge:
    build: .
    container_name: ep2500-bridge
    restart: unless-stopped
    environment:
      EP2500_ID:    "YOUR_DEVICE_ID"
      EP2500_IP:    "YOUR_DEVICE_IP"
      EP2500_KEY:   "YOUR_LOCAL_KEY"
      EP2500_PORT:  "6668"
      MQTT_HOST:    "YOUR_BROKER_IP"
      MQTT_PORT:    "1883"
      MQTT_USER:    ""
      MQTT_PASS:    ""
      GRID_SOURCE:  "http"
      ECO_URL:      "http://YOUR_METER:PORT/v1/json"
      ECO_FIELD:    "power"
      ECO_INTERVAL: "5"
      ECO_SPIKE:    "1500"
      LIMIT_MAX:    "800"
      LIMIT_SAFE:   "300"
      LOG_DPS:      "1"
      TZ:           "Europe/Berlin"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Mind the indentation – in YAML it defines the structure. Check with
`docker compose config` that the file parses.

**On the meter source:** `GRID_SOURCE: "http"` polls a meter directly over
HTTP. It expects a JSON field holding the power, positive when importing,
negative when exporting. For a different source set it to `mqtt` and push the
value to a topic – then `GRID_TOPIC` applies.

## 5.4 Start it

```bash
docker compose up -d --build
docker logs -f ep2500-bridge
```

The log should confirm the MQTT connection, the published discovery configs
and 83 datapoints read from the device. It then appears in HA under
Settings → Devices & Services → MQTT. No HA restart needed.

*Note: the bridge logs and entity names are in German, since that is what I
built it for. Everything is in one file and easy to translate if you prefer
– the datapoint mapping itself is language-independent.*

`LOG_DPS=1` logs changes to settings datapoints – useful while exploring,
optional in normal operation.

---

# Part 6: dashboard

The entities are usable straight away. For a tidy view use `dashboard.yaml` from the repository: create a dashboard, pencil icon top right,
three-dot menu, **Raw configuration editor**, paste.

Check the entity IDs if cards stay empty – HA derives them from the names, and
umlauts are not always transliterated the same way. Look under Settings →
Devices & Services → Entities, filter "ep2500".

The event log card only scrolls with **card-mod** from HACS:

```yaml
card_mod:
  style: |
    ha-card {
      max-height: 400px;
      overflow-y: auto;
    }
```

Without card-mod the card simply grows; in that case limit the number of
entries shown.

---

# Part 7: tuning the controller

## 7.1 Initial settings

In the dashboard under Control:

- **Battery charge limit:** 2500 W. See part one – never leave it at 0.
- **Grid target:** 0 for zero export. To export deliberately, for instance to
  feed a second storage unit, enter a negative value.
- **Backflow prevention:** off, unless you want to block export entirely.
- **Control active:** on.

## 7.2 Settings that worked for me

| Parameter | Value | Why |
|---|---|---|
| Interval | 5–6 s | below that you fight the device's own response time |
| Gain | 1.0 | no need for more, above that it oscillates |
| Deadband | 15–25 W | filters out ineffective small corrections |
| Max step | 800 W | matches the control range |

On the deadband: every control step writes to the inverter's memory. At a 5 s
interval that would be 17,000 writes a day in theory. Nobody knows how well the
firmware handles that long term. A generous deadband is not a loss of accuracy,
it is caution.

## 7.3 What you should see

The meter hovers around zero, the feed-in limit follows your consumption, and
on PV surplus it drops back to 0 so the battery charges. Direction changes and
faults show up in the event log.

---

# The off-grid socket

Important if you plug anything in there: **the off-grid load does not appear in
the AC output power (DP 155) and not at the meter.** It runs on a separate path
and is invisible to a zero-export controller.

Measured example: 119 W at the AC output, 298 W off-grid load at the same
moment, and the battery delivering 471 W. The difference is conversion losses
across two paths.

So if you estimate remaining runtime, do it from the battery power (DP 128),
not from the AC output.

The socket is switched via DP 119. It wakes the inverter from standby – which
costs standby consumption even with nothing plugged in.

Whether it also accepts export, from a balcony PV system for example, I have
not tested. On the type plate the off-grid terminal is listed as output only.

---

# Pitfalls

**One connection only.** The device accepts a single local connection. Open the
app and the bridge loses it, and vice versa. Stop the bridge for hands-on
tests.

**Delta frames.** After the initial full status the device sends only changed
datapoints. Anything reading with `receive()` must merge frames into a cache,
otherwise half the values appear to be missing. The bridge handles this.

**16-bit overflows.** Under weak irradiance DP 143 occasionally returns 65535 –
that is −1 as a signed 16-bit integer. The app displays it unfiltered, and the
yield counter DP 125 accumulates it. On an overcast morning mine read 9026 Wh
where perhaps 800 was possible. The bridge filters such values; for a usable
daily yield, build your own counter in HA: an integration sensor (left Riemann
sum, time unit **hours**) on the PV power, with a utility meter on top set to a
daily cycle.

**Anti-windup is mandatory.** With an empty battery the device delivers
nothing, whatever limit you set. A naive controller adds the error again on
every pass and writes for hours with no effect.

**The 800 W is a software limit.** I accidentally wrote 1000 and the device
applied it. The type plate says "800 W (default) / 2500 W (premium)". If you
control it, you have to cap it yourself.

**Do not write blindly through the DP numbers.** On a grid-tied inverter, grid
parameters and country settings live in the same address space. Only touch DPs
whose meaning you have narrowed down, and note the original value first.

**The bundled smart meter** is an Acrel ADL200W-CTWF. Per the manufacturer the
device's own zero-export function only works with that meter; third-party
support is planned for future updates. With any other meter you need an
external controller like this one.

---

# Identifying datapoints yourself

If you want to chase the remaining unknowns:

The most effective trick is a service that holds the connection permanently and
logs every change – `LOG_DPS=1` in the bridge does exactly that. Open the app
and it loses the connection; close the app and it re-reads the full status and
compares against its cache. The log then tells you precisely which DP moved as
a result of your change in the app.

That is how I mapped the entire schedule structure without a single blind write
to an unknown datapoint.

For measurements: log overnight. Cell voltages fall slowly and monotonically,
temperatures cool down – that is how I identified DP 130 to 133.

---

# Open questions

- Has anyone tested DP 120 ("anti-backflow grid regulation")? I suspect it is
  the setpoint for the device's own zero-export function with the Acrel meter.
- Any other enum values for the operating mode besides `grid_priority` and
  `backup_power`?
- Can the Acrel ADL200W be emulated? Its datasheet lists Modbus TCP over Wi-Fi.
  That would let you feed the EP2500 any meter data and leave the control loop
  to the device.
- Does grid charging work for you with no PV modules connected? That would make
  the EP2500 usable as a pure AC-coupled storage unit.

---

No warranty of any kind – you are writing to undocumented datapoints of a
grid-tied inverter, and you do so at your own risk.

And the note that always comes up eventually: multiple plug-in solar devices at
one grid connection point are counted together. Different phases or circuits do
not multiply the 800 W allowance.
