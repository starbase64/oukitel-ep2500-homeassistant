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

## Per-phase readings

The Eco Tracker reports the balanced sum and each phase separately. The
zero-export controller keeps using its own averaged value, unchanged; the
phases are a separate, smoothed set used for the checks described in Part 7.

Two things they give you that the sum cannot. **What else sits on the device's
phase**: subtracting the EP2500's own contribution (DP 137) from its phase
leaves everything else, which may well be feeding rather than consuming.
**Where compensation happens**: when the sum does not follow a change, the
phases show which one moved instead.

Set the phase the EP2500 is wired to from the dashboard; the default is 1.

**Requires `GRID_SOURCE=http`.** The per-phase values come from the Eco
Tracker's own endpoint. With `GRID_SOURCE=mqtt` only the balanced total
arrives, so the phase display and AC surplus charging stay inactive; the
bridge says so at startup.

A line lands in the log once a minute:

```
Phases | L1  +603 | L2   +87 | L3  -334 | sum  +356 W | own L1, others -124 W
```

`PHASE_LOG_INTERVAL` controls the cadence, 0 turns it off. The readings also
go to Home Assistant continuously, but the recordings are what you read
afterwards when working out why the controller did what it did.

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

For an installation upgraded from the old German entity names, use the bundled
registry-aware converter instead of changing dozens of dashboard rows by hand:

```bash
cd /path/to/oukitel-ep2500-homeassistant
python3 tools/fix_dashboard_ids.py \
  /path/to/homeassistant/config \
  dashboard.yaml dashboard.local.yaml
```

The second argument is the directory that holds `configuration.yaml` — the
tool reads `.storage/core.entity_registry` below it. Prefix the command with
`sudo` if that directory belongs to the container user.

The generated `dashboard.local.yaml` uses the entity IDs that actually exist
on that HA installation. If MQTT entities are reported missing, restart the
bridge so that it republishes discovery and run the command again.

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

## The export ceiling

`LIMIT_MAX` sets it at startup and the dashboard field **Export limit max**
changes it at runtime, up to `LIMIT_HW_MAX`. Everything respects it: the
zero-export controller, a meter target and pass-through, which pins the limit
to exactly this value.

The default of 800 W is the German limit for a balcony system. Other countries
and other installations allow different figures, which is why the field exists
- but what is permitted at your connection is your responsibility, not the
bridge's. The bridge will put out whatever you enter.

One thing it cannot do: exceed the ceiling to compensate for loads on its own
phase. If 200 W is consumed on that phase and the ceiling is 800 W, the export
reaching the meter is 600 W. Raising the ceiling is the only way to change
that, with the caveat above.

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

## 7.4 Charging a second AC storage, and why it needs a guard

A negative meter target keeps a deliberate export so a neighbouring
AC-coupled storage unit sees surplus and starts charging.

Left alone this runs away. The neighbour absorbs the export, the meter returns
towards zero, the controller gives more, the neighbour takes more. Both ramp
until one hits its ceiling. Measured on 12.09. with a target of -300 W and a
Bluetti Balco260 brought onto phase 3:

```
L1  -286 | L3   +11 | sum  -265 W    neighbour not connected yet
L1  -384 | L3  +267 | sum  -108 W    neighbour absorbing
L1  -739 | L3  +673 | sum   -55 W    neighbour absorbing nearly all
```

L1 fell by 453 W, L3 rose by 662 W, and the balanced sum moved the wrong way.
Export reached the 800 W ceiling and the EP2500 ended up discharging its own
battery into the neighbour.

**The meter alone cannot tell the two cases apart.** Whether a phase reads
negative because of PV surplus or because a battery is discharging looks
identical. What does work is checking whether the meter follows the device's
own change: raise export by one step, wait, and see whether the balanced sum
moved down by a fair share of what was actually put out.

Raising export is therefore a probe; lowering is free and immediate. If the
sum does not follow, the export ceiling stays at the value from before the
step and an event says why:

```
Negative meter target capped at 285 W - exported +132 W more but the meter
only moved +1 W (1 %), so something else is absorbing it
```

Changing the target releases the cap. So does the dip test below.

### Which reading the target follows

A non-zero target can be regulated against either reading. Three settings,
selectable from the dashboard.

**Balanced sum** (default) is what the meter bills. A storage unit on any
phase can absorb the export, so the target may simply be unreachable - the
probe and the cap above exist for exactly that.

**Own phase** regulates the phase the EP2500 is wired to. A unit on a
different phase cannot influence that reading at all, so the loop is not
detected, it is impossible. Ask for -800 W and the device exports whatever it
takes to make its own phase read -800 W, even 1000 W if 200 W of house load
sits on that phase.

**Automatic** (default) is the balanced sum until the state of charge reaches
the pass-through threshold, and the own phase from there on. That is where the
distinction actually matters.

Below the threshold the battery is still being filled, so export may well be
coming out of it - the balanced sum is the honest reading and the probe has
something to catch. At and above it the battery is held, the exported energy
is PV, and local loads on the device's own phase are quietly eating into what
was meant to reach the other storage unit. Compensating for them is exactly
what is wanted, even if it means putting out 1000 W to leave 800 W on the
phase.

Note that "full" here means the pass-through threshold, not the charge stop.
The device must never actually reach 100 %, so that is the only sensible
reading of full.

The switch is logged both ways:

```
Meter target now follows phase L1 - battery at 90 %, local loads on that
phase are compensated
```

**The trade-off is the bill.** In own-phase mode the balanced total is no
longer the reference. If the other phases import while this one exports, the
meter can sit in import while the dashboard shows the target met. With a
balanced meter that means buying electricity to charge the storage unit. Use
it when there is genuine surplus, not as a permanent setting.

Two details. A target of exactly 0 always uses the balanced sum - zero export
is a statement about the meter, not about one phase. And if the phase reading
goes stale, the controller falls back to the sum and says so, rather than
regulating against a value that stopped moving.

The probe and the dip always measure whatever the controller is steering. In
own-phase mode they judge the phase, so they still catch a neighbouring unit
that happens to sit on the *same* phase - the one case where the loop can
still occur.

### Telling a storage unit from a real load

A cap should not last forever - the neighbour may have filled up or been
unplugged, and a real load may have appeared that genuinely wants the power.
Testing that by pushing export back up is expensive: every watt of the test
goes into the neighbour's battery if the suspicion was right.

Lowering export answers the same question for free. Every `NEG_RETRY` seconds
the controller withholds `NEG_DIP` watts for one settling period and watches
who follows:

| | What the meter does | Conclusion |
|---|---|---|
| Real load | moves towards import by the full amount withheld | not a storage unit, cap released |
| Storage unit | barely moves, it simply charges less | still tracking us, cap held |

```
Negative meter target released - held back 150 W and the meter followed by
150 W (100 %), so this is a real load and not a storage unit
```

This also covers the case where both are present: you ask for -200 W, a
storage unit takes it, and then a real consumer switches on as well. The dip
separates the two, and export is allowed to rise again for the part that is
genuinely being used.

Moving back up to a level already reached is not treated as a probe. Without
that exception the return from each dip would be measured as an increase,
fail its own check, and ratchet the ceiling down step by step.

Be clear about what this does and does not achieve. It stops the escalation.
It cannot make the neighbour take exactly the amount you asked for - that unit
takes what it sees, and with a balanced meter the target is simply not
reachable while another controller works against it. Hitting a precise figure
would need a measurement at the neighbour's connection, which is exactly the
dependency this project avoids.

`PROBE_SETTLE` has to outlast the neighbour's reaction. See "How long a
neighbour takes to react" below.

---

## How long a neighbour takes to react

Anything that judges its own effect on the meter has to wait longer than the
other controller needs to respond. Measured on 12.09. against a Hoymiles
4020X on another phase, with the EP2500 drawing 741 W:

| Time after the step | Neighbour covering | Share still at the meter |
|---|---|---|
| 19 s | nothing | 74 % |
| 79 s | 640 W | 14 % |

A verdict at 25 seconds would have read "genuine" and walked straight into the
loop. `PROBE_SETTLE` therefore defaults to 90 seconds. The response time also
varies - in a second run the neighbour had already reacted after 19 seconds -
so do not tune this to the shortest observation you happen to make.

---

## Backup charging

One switch, manual only. It pauses the controller, writes `backup_power` to
DP 165, and sets DP 122 to the configured power. The device then charges from
the grid unconditionally, regardless of the meter.

It stops by itself at the charge stop, because leaving the device sitting at
100 % costs harvest for hours. Switching it off restores the mode, the
previous charge limit and the controller.

Measured throughput: 800 W from the grid produces about 727 W into the
battery, so roughly 91 % conversion.

---

## AC surplus charging

The mirror image of the section above: instead of creating export for someone
else, the EP2500 absorbs surplus nobody else is taking.

It uses the same probe. Confirmation takes two minutes before it engages, then
the charge limit rises one step per `PROBE_SETTLE`, each step verified. Coming
down is immediate and never waits. That makes the loop asymmetric in the safe
direction and deliberately unhurried.

If a neighbouring battery is producing the "surplus", the probe sees it within
one step and stops:

```
AC surplus charging stopped - another source compensated: drew +150 W,
meter moved +0 W (0 %)
```

Surplus charging and a negative meter target cancel each other out - one
creates export, the other absorbs it. Running both is a loop, so the
combination is refused rather than silently prioritised.

Priority overall: backup beats everything, then pass-through, then surplus
charging, then zero-export control. Surplus charging hands over at the
pass-through threshold rather than at the charge stop - carrying on past it
would walk the battery into the 100 % shutdown that pass-through exists to
prevent.

If the meter goes quiet, surplus charging stops. It refuses to act on a
reading older than `GRID_MAX_AGE`, because a frozen value would otherwise let
it keep raising the charge limit against a meter that has said nothing for
hours.

---

## The Shelly plugs

Up to four are supported. **None of them feeds the control loop.** They exist
for two things: watching power independently of what the device reports, and
cutting a unit from mains remotely when something goes wrong.

| Slot | Typical use |
|---|---|
| `SHELLY_IP` | in front of the EP2500 - independent check on DP 155, and a hard mains disconnect |
| `SHELLY2_IP` | anything else worth watching. Set `SHELLY2_ON_OFFGRID` when it hangs on the off-grid outlet itself - it is then not polled while the outlet is dead, instead of logging a failure every few seconds |
| `AC_STORAGE1_IP`, `AC_STORAGE2_IP` | AC-coupled storage units - remote emergency shutdown |

The addresses are editable from the dashboard and stored as retained MQTT, so
they survive a restart. Leave one empty and that slot is simply inactive.

Switching the first one off disconnects the EP2500 from mains, and the bridge
loses its connection while it is off - that is the point of it.

---

# The off-grid outlet

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

The dashboard therefore carries two entries. **Off-grid outlet (permission)**
is DP 119, what you asked for. **Off-grid output live** is derived from DP 140
and says whether current is actually flowing.

Switching the outlet on while the battery is too empty is the common case: the
sunrise automation fires, the device accepts the command and does nothing,
and the outlet stays dead until the battery reaches its release threshold -
which on a dull morning can be most of the day. Observed on 16.09.: enabled at
sunrise at 13 % state of charge, still dead hours later.

That is reported after `OFFGRID_MISMATCH_S` and repeated every
`OFFGRID_REMIND_S` while it lasts, with the elapsed time in the message. A
single line at dawn is too easy to miss.

They come apart in practice. Measured on 13.09.: DP 140 dropped to 0 while
DP 119 stayed on and was never written - the device withdrew the output on its
own as the battery ran down. Without the second entry the dashboard shows a
socket that has been dead for an hour.

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

DP 135 looks like the obvious readout for the socket's real state and is not:
it sits at 231 to 244 V no matter what, including overnight in standby with
the output off. A watchdog built on it produced nothing but false alarms and
was removed again.

**DP 140 (off-grid current) is the one that works.** With the socket live it
reads 0.2 to 0.3 A even with nothing plugged in, and it drops to 0 the moment
the output is switched off. DP 119 is no help here either — it only shows up
in the log when it changes, so its silence says nothing about the current
state.

## What idling costs

Once the device reaches the discharge-stop SoC it goes to standby and stops
feeding, but it keeps draining the battery. Two nights measured at 14–15 %
state of charge, one with the off-grid outlet energised and one without:

| Night | Off-grid outlet | Minutes per point | Self-consumption |
|---|---|---|---|
| 09./10.09. | on | 79–80 (six transitions) | ~15.6 W |
| 11./12.09. | off | 285 (one transition) | ~4.3 W |
| 12./13.09. | off | over 328, no transition at all | under 3.8 W |

The energised socket therefore costs roughly **12 W**, about 70 % of the idle
draw — for an output with nothing plugged into it. Over a twelve-hour night
that is around 130 Wh, more than the pack still holds at 15 %.

A percentage point is `BATT_WH / 100`, so 20.5 Wh with the default of 2048.
Watch the numbers: an earlier revision of this document put the figure at 38 W
because it assumed a 5 kWh pack.

One caveat. The two figures with the socket off rest on very little: one
percentage point in the first case and none at all in the second, which only
gives an upper bound. Both agree that it is under 5 W. And both runs sit at 14–15 %, where
the LiFePO4 curve is flat and the BMS estimate of one point is coarse.

Worth being clear about: the discharge stop protects the battery from the
*controller*, not from the device itself. Over a long spell of bad weather the
pack keeps draining below it.

To repeat the measurement, no extra sensors are needed. Take the state of
charge while the device sits in standby and time one percentage point. Use
DP 140 to confirm which state the socket was actually in — it reads 0 only when
the output is off.

## Switching it off overnight

Four automations and one helper. This belongs in Home Assistant rather than
in the bridge — the bridge has no idea where you live or when the sun sets.
`homeassistant/offgrid_night.yaml` contains the automations and
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
- id: ep2500_offgrid_sunset
  alias: EP2500 - off-grid outlet off at sunset
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
        message: Off-grid outlet switched off at sunset.

- id: ep2500_offgrid_sunrise
  alias: EP2500 - off-grid outlet on at sunrise
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
        message: Off-grid outlet switched on at sunrise.

- id: ep2500_offgrid_apply_now
  alias: EP2500 - align off-grid outlet when the helper changes
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

- id: ep2500_offgrid_helper_off
  alias: EP2500 - restore off-grid outlet when night switch is disabled
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

**Check the entity ID before relying on it.** An automation that points at an
entity which does not exist fails silently - no error, no log line, nothing
happens at sunset. On an installation upgraded from German entity names the
socket is `switch.oukitel_ep2500_off_grid_steckdose`, not `..._off_grid_socket`.
Developer tools > States tells you which one you have.

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
