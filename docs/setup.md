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
git clone https://github.com/starbase64/oukitel-ep2500-homeassistant.git
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
      ECO_SPIKE:    "600"
      LIMIT_MAX:    "800"
      LIMIT_SAFE:   "0"
      METER_TARGET_MIN: "-2000"
      METER_TARGET_MAX: "2000"
      AC_STORAGE1_IP: ""
      AC_STORAGE2_IP: ""
      AC_STORAGE_MAX_AGE: "15"
      AC_STORAGE_NOISE: "5"
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

## Recording the log from the dashboard

The **Protokoll aufzeichnen** switch tells the bridge to mirror everything it
logs into a file under `REC_DIR`. A sensor next to it shows how long the
recording has been running and how large it is.

There is no download button, because a Home Assistant button can only send a
command — it cannot hand you a file. The bridge therefore serves the
recordings over HTTP on `REC_PORT`, and the dashboard shows a link to that
page. Downloading works while a recording is still running; the file is
flushed after every line.

Two things to be aware of. The server has **no authentication** and will hand
any file in `REC_DIR` to anyone who can reach the port, so keep it on the home
network and do not forward it through a router. It only serves paths matching
its own naming pattern, so it cannot be walked out of that directory, but that
is not a substitute for keeping the port private.

`REC_KEEP` bounds how many recordings are retained; older ones are deleted
when a new recording starts. `REC_MAX_MB` closes a recording that grows past
the limit rather than filling the disk. Mount `REC_DIR` as a volume, otherwise
the recordings disappear with the next container rebuild.

## A negative meter target and a second AC storage

A negative meter target tells the controller to keep a deliberate export at
the meter. That is how you feed an AC-coupled storage unit that only starts
charging when it sees surplus.

Left alone, that would run away. The EP2500 exports to create surplus, the
other unit absorbs it, the meter reads zero again, so the EP2500 exports more.
Both units ramp until one of them hits its limit.

The Shelly plugs in front of the AC storage units break that loop. Their
measured charging power is subtracted from the request, so the target only
asks for what is not already being absorbed:

```
requested -300 W, storage charging 0 W    -> effective target -300 W
requested -300 W, storage charging 100 W  -> effective target -200 W
requested -300 W, storage charging 300 W  -> effective target    0 W
requested -300 W, storage charging 500 W  -> effective target    0 W
```

The contribution is capped at the request, so the compensation can never turn
an export request into an import one.

Two guards sit around it. A reading older than `AC_STORAGE_MAX_AGE` or a
Shelly whose relay is off means the surplus has no taker, so the negative
target is suspended and the controller falls back to zero export. And
`AC_STORAGE_NOISE` ignores the few watts a plug reports when nothing is
charging.

### Worked example

Discharge stop 15 %, pass-through target 90 %, meter target -300 W, one AC
storage on a Shelly. PV is strong.

| Step | Meter | Storage | Effective target | EP2500 export | Why |
|---|---|---|---|---|---|
| 1 | +40 W | 0 W | -300 W | rises | surplus has to be created first |
| 2 | -290 W | 0 W | -300 W | steady | the other unit has not noticed yet |
| 3 | -180 W | 110 W | -190 W | steady | it started; the request shrinks by what it takes |
| 4 | -10 W | 290 W | -10 W | steady | nearly everything is absorbed |
| 5 | 0 W | 300 W | 0 W | steady | balance reached, no runaway |
| 6 | +260 W | 0 W | -300 W | rises | the other unit is full and stopped |

Nothing escalates in step 5 because the export the EP2500 produces shows up in
the Shelly reading and is deducted again.

### What happens when the EP2500's own battery fills up

Pass-through would pin the export limit at `LIMIT_MAX`, which overrides your
meter target. It therefore stays out of the way while a target is set — but
only up to a point. `PASS_OVERRIDE` points above the pass-through target,
protection wins and pass-through engages anyway, with an event saying so.

With the defaults that means: at 90 % nothing happens beyond a note in the
event log that protection is being held back. At 93 % pass-through takes over
and the meter target is suspended until the state of charge is back at 90 %.
Running the device into its 100 % shutdown costs far more harvest than missing
a meter target for half an hour.

## Pass-through: how it holds the state of charge

Because there is no direct PV-to-grid path, a steady state of charge simply
means PV charge power equals export power. Pass-through therefore sets two
fixed values rather than running a balance controller: the export limit goes
to `LIMIT_MAX` and stays there, and DP 156 (PV charge power) starts at the
same figure.

Only the conversion loss is trimmed away, and it is trimmed against the state
of charge, not against battery power. The state of charge is the quantity you
want to hold, it moves slowly, and it arrives on time. Battery power (DP 128)
lags by 20 to 70 seconds; using it as the control variable makes the loop
oscillate between full charge and full discharge.

The trim is one small step every few minutes, proportional to how far the
state of charge sits from the target and symmetric in both directions. It is
skipped upward when the PV limit is not the binding constraint — under cloud
the panels deliver less than the limit allows, and raising it further would
only cause an overshoot when the sun returns.

Set the target with `Pass-through from SoC`. Pass-through engages at that value
and then holds it, dropping back to zero-export control five points below.

The PV charge power is capped at twice `LIMIT_MAX` while pass-through runs.
It has to sit somewhat above `LIMIT_MAX` to cover the conversion loss — 800 W
into the battery yields less than 800 W at the output — but it never needs to
go far beyond that, and the cap keeps a stuck loop from opening the solar side
wide.

Pass-through also requires a fresh meter value. On a meter outage the bridge
ends pass-through, reopens the PV limit and sets the export limit to
`GRID_FAIL_LIMIT`. This avoids continuing to export blindly from the last
known command.

While pass-through is active the harvest is capped at `LIMIT_MAX`. That is
unavoidable on this device: the alternative is letting the battery reach the
charge stop, where it shuts the MPPTs down and restarts about five percent
lower.

**On the meter source:** `GRID_SOURCE: "http"` polls a meter directly over
HTTP. It expects a JSON field holding the power, positive when importing,
negative when exporting. For a different source set it to `mqtt` and push the
value to a topic – then `GRID_TOPIC` applies.

Both sources run through the same filter: values beyond `ECO_ABSURD` are
dropped outright, and a jump larger than `ECO_SPIKE` against the last valid
reading is only accepted once the next reading confirms it. A single outlier
would otherwise drive the controller to its limit. If your MQTT source
publishes rarely, raise `ECO_SPIKE` or set it to `0` to switch the jump
detection off – while a jump waits for confirmation the stored value ages, and
past `GRID_MAX_AGE` (60 s) the controller treats the meter as failed.

## 5.3 Start it

```bash
docker compose up -d --build
docker logs -f ep2500-bridge
```

The log should confirm the MQTT connection, the published discovery configs
and 83 datapoints read from the device. It then appears in HA under
Settings → Devices & Services → MQTT. No HA restart needed.

The bridge publishes English entity names. Existing installations upgraded
from an older German-language release keep their entity IDs; the README
explains the migration options.

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
- **Meter target (+ import / - export):** `0 W` for zero export. A positive
  value such as `+50 W` deliberately keeps that much grid import. A negative
  value such as `-300 W` makes the EP2500 increase its output until the meter
  reports about 300 W export — useful when a second AC-coupled storage unit
  should charge even though the EP2500 is not yet full. The EP2500 can only
  move the meter as far as its configured `LIMIT_MAX` and available battery/PV
  power allow.
- **AC storage 1/2:** enter the IP addresses of the Shelly plugs in front of
  the AC-coupled storage units and switch their relays on. Positive Shelly
  power is treated as charging power. Leave an unused address empty.
- **Backflow prevention:** off, unless you want to block export entirely.
- **Control active:** on.

## 7.2 Settings that worked for me

| Parameter | Value | Why |
|---|---|---|
| Interval | 5–6 s | below that you fight the device's own response time |
| Gain | 1.0 | no need for more, above that it oscillates |
| Deadband | 15–25 W | filters out ineffective small corrections |
| Max step | 800 W | matches the control range |

On the deadband: every effective control step sends another write command to
the inverter. How the firmware persists these values is undocumented. A
generous deadband reduces unnecessary traffic without sacrificing useful
accuracy.

## 7.3 What you should see

The meter hovers around zero, the feed-in limit follows your consumption, and
on PV surplus it drops back to 0 so the battery charges. Direction changes and
faults show up in the event log.

## 7.4 Charging another AC storage without controller ramp-up

A fixed negative grid target alone creates a feedback problem: with a target
of `-300 W`, the other storage absorbs the first 300 W and the grid meter moves
back towards zero. A controller that only sees the grid meter then raises the
EP2500 output again.

The two optional AC-storage Shelly inputs remove that feedback. For a negative
requested target the bridge uses:

```text
effective meter target = requested target + measured AC-storage charging power
```

The measured contribution is clamped to the requested export. With `-300 W`
requested, 100 W of storage charging produces an effective meter target of
`-200 W`; at 300 W charging the effective target is `0 W`. The sum of captured
charging power and remaining grid export therefore stays at 300 W instead of
ramping indefinitely.

If at least one AC-storage Shelly is configured but no enabled unit has a fresh
measurement, the bridge temporarily changes a negative target to `0 W`. It
will not deliberately export blind. The original negative-target behaviour is
retained when no AC-storage Shelly is configured at all.

Pass-through is suspended while a non-zero meter target is active because its
fixed-output strategy would otherwise override this controller.

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

## The switch shows the permission, not the state

DP 119 is the setting "off-grid output enabled". It is not a readout of
whether the socket is actually live. The device can have the output shut down
anyway and DP 119 still reads true. Home Assistant then shows the switch on
while nothing comes out of the socket.

The main reason is a lockout tied to the state of charge. **The device only
releases the off-grid output once the state of charge is five points above the
discharge stop.** Measured on 11.09.: discharge stop at 15 %, and both the
socket and normal control came back at 20 %. Below that the device accepts
DP 119 = true without complaint and simply leaves the output dead — which
looks exactly like a bug in the bridge until you know about it.

The bridge knows the rule now. Switching the socket on below the threshold
still sends the command, but logs why it will not take effect yet, and the
watchdog names the state of charge instead of blaming the device. If your unit
uses a different margin, set `OFFGRID_SOC_MARGIN`.

Every switch command is read back from the device a few seconds later. The
EP2500 acknowledges commands it does not carry out, and without the read-back
the requested value would sit in the cache and in Home Assistant.

What the bridge cannot do is show you the real state of the socket, because
no datapoint reports it. DP 135 looks like the obvious candidate and is not:
it sits at 231 to 237 V no matter what, including overnight in standby with
the output off. A watchdog built on it produced nothing but false alarms and
was removed again. The state-of-charge lockout above is the one statement
that holds up, so that is what gets reported.

## What idling costs

Once the device reaches the discharge-stop SoC it goes to standby and stops
feeding, but it keeps draining the battery. Over one night (09./10.09.) the
state of charge fell from 15 % to 9 % at a steady one point per 79 to 80
minutes — about 38 W on a 5 kWh pack. Remarkably linear across six hours.

The socket was live throughout: DP 135 sat between 231 and 236 V while DP 140
read 0 A. So the inverter was running an AC output for a socket with nothing
in it. How much of the 38 W that accounts for is exactly what the automation
below is meant to measure.

Worth being clear about: the discharge stop protects the battery from the
*controller*, not from the device itself. Over a long spell of bad weather the
pack keeps draining below it.

## Switching it off overnight

Four automations and one helper. This belongs in Home Assistant rather than
in the bridge — the bridge has no idea where you live or when the sun sets.
`homeassistant/offgrid_nacht.yaml` contains the automations and
`homeassistant/input_boolean.yaml` contains the helper ready to include.

### configuration.yaml

```yaml
input_boolean:
  ep2500_offgrid_night_off:
    name: EP2500 off-grid off at night
    icon: mdi:weather-night
```

**If you already have an `input_boolean:` block, merge the indented entry into
it.** A second `input_boolean:` key at the top level makes Home Assistant
refuse to start with a duplicate key error. You can also create the helper
through Settings > Devices & Services > Helpers instead; pick type "Toggle"
and the entity ID will match.

### automations.yaml

```yaml
- id: ep2500_offgrid_sonnenuntergang
  alias: EP2500 - off-grid socket off at sunset
  mode: single
  triggers:
    - trigger: sun
      event: sunset
  conditions:
    - condition: state
      entity_id: input_boolean.ep2500_offgrid_night_off
      state: "on"
  actions:
    - action: switch.turn_off
      target:
        entity_id: switch.oukitel_ep2500_off_grid_socket
    - action: logbook.log
      data:
        name: EP2500
        message: Off-grid socket switched off at sunset.

- id: ep2500_offgrid_sonnenaufgang
  alias: EP2500 - off-grid socket on at sunrise
  mode: single
  triggers:
    - trigger: sun
      event: sunrise
  conditions:
    - condition: state
      entity_id: input_boolean.ep2500_offgrid_night_off
      state: "on"
  actions:
    - action: switch.turn_on
      target:
        entity_id: switch.oukitel_ep2500_off_grid_socket
    - action: logbook.log
      data:
        name: EP2500
        message: Off-grid socket switched on at sunrise.

- id: ep2500_offgrid_sofort
  alias: EP2500 - align off-grid socket when the helper changes
  mode: single
  triggers:
    - trigger: state
      entity_id: input_boolean.ep2500_offgrid_night_off
      to: "on"
    - trigger: homeassistant
      event: start
  conditions:
    - condition: state
      entity_id: sun.sun
      state: below_horizon
  actions:
    - action: switch.turn_off
      target:
        entity_id: switch.oukitel_ep2500_off_grid_socket

- id: ep2500_offgrid_helfer_aus
  alias: EP2500 - restore off-grid socket when night switch is disabled
  mode: single
  triggers:
    - trigger: state
      entity_id: input_boolean.ep2500_offgrid_night_off
      to: "off"
  actions:
    - action: switch.turn_on
      target:
        entity_id: switch.oukitel_ep2500_off_grid_socket
```

The syntax above is for Home Assistant 2024.10 and later. On older versions
use `trigger:`, `condition:` and `action:` in the singular.

The startup trigger also aligns the socket after a Home Assistant restart.
Disabling the helper restores the socket immediately, while sunrise restores
it only if automatic night switching is still enabled.

The `logbook.log` entries are optional. They make it easy to line up a
measurement night with what actually happened.

Add a row for `input_boolean.ep2500_offgrid_night_off` to the controls
card in `dashboard.yaml` so the switch sits next to the socket it governs.

### Checking whether it helped

No extra sensors needed. Take the state of charge while the device sits in
standby and measure how long one percentage point takes. Before: 79 to 80
minutes. If that roughly doubles, the socket was about half the idle draw.

The comparison only works if the battery reaches the discharge stop in the
evening and stays there overnight, so pick a night after a weak solar day.

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
