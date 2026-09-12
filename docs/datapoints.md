# Oukitel EP2500 – Tuya datapoints (DP)

Derived from local communication via TinyTuya (protocol 3.5) and cross-checked
against the Smart Life app display. Status: September 2026, firmware per
DP 107/108 = 1.02 / 1.17.

The device reports 83 datapoints. No data model is registered in the Tuya cloud
(`functions: []`), so all mappings below were established by observation.

*A note on the app labels: my app runs in German, so the English labels quoted
here are my translations. If yours differ slightly, go by the position in the
menu.*

---

## Confirmed – control (writable)

| DP | Meaning | Unit | App label | Confidence |
|---|---|---|---|---|
| 118 | Backflow prevention – blocks **export** | bool | Anti-backflow | certain |
| 119 | Off-grid socket on/off | bool | – | certain |
| 120 | Anti-backflow grid regulation | W | same | meaning unclear |
| 121 | Max. feed-in power | W | Maximum allowable feed-in power | certain |
| 122 | Max. battery charge power **total** (PV + grid) | W | Maximum allowable battery charging power | certain |
| 123 | Charge stop SoC | % | Charge stop SOC | certain |
| 124 | Discharge stop SoC | % | Discharge stop SOC | certain |
| 156 | PV charge power currently in effect | W | – | certain |
| 157 | PV charge power 2 (schedule) | W | PV charging power 2 | certain |
| 162 | PV charge power 3 (other time) | W | PV charging power 3 | certain |
| 158 / 159 | PV schedule 1: start / end | h | Time1 start / over time | certain |
| 160 / 161 | PV schedule 2: start / end | h | Time2 start / over time | certain |
| 170 / 171 | Mode schedule 1: start / end | h | Time1 | certain |
| 172 / 173 | Mode schedule 2: start / end | h | Time2 | certain |
| 174 / 175 | Mode schedule 3: start / end | h | Time3 | certain |
| 176 / 177 | Mode schedule 4: start / end | h | Time4 | certain |
| 165–169 | Operating mode 1–5 (169 = other time) | enum | Operating mode 1–5 | certain |

Known enum values for the operating mode: `grid_priority`, `backup_power`.
There may be more that were not triggered during testing.

**Note on DP 156:** the value follows whichever schedule slot is currently
active. It is the PV charge power actually in effect. No separate datapoint was
found for PV charge power 1 – possibly identical with 156.

**Note on DP 119 – off-grid socket:** this is the permission, not the state.
The device releases the output only once the state of charge is five points
above the discharge stop (DP 124). Below that it acknowledges a write and
leaves the socket dead. DP 119 also only appears in the log when it *changes*,
so its silence says nothing about the current state.

**DP 140 (off-grid output current) is the readout that does tell you.** With
the socket live it sits at 0.2 to 0.3 A even with nothing plugged in; the
moment the output is switched off it drops to 0 and stays there. In one
measurement run the transition is a single line at sunset:

```
19:53:42  DP 140  Off-grid current  2 -> 0
```

**DP 135 (off-grid voltage) is not a state readout**, despite looking like the
obvious candidate. Across four measurement runs it never left the 231–244 V
band — in standby, with the output off, and on an empty battery alike. It
appears to report an internal bus voltage rather than the socket. A watchdog
built on it produced nothing but false alarms.

Note the combination while the socket is idle: DP 135 around 235 V, DP 140 at
0.25 A, and DP 141/142 at 0 W. Roughly 60 VA of apparent power with no real
load reported — the inverter holding an energised output for an empty socket.

**Note on standby self-consumption:** after reaching the discharge-stop SoC the
device goes to standby and stops feeding, but keeps draining the battery. Two
nights measured at 14–15 % state of charge, with `BATT_WH` of 2048 one point
equals 20.5 Wh:

| Night | Off-grid socket | Minutes per point | Self-consumption |
|---|---|---|---|
| 09./10.09. | on | 79–80 (six transitions) | ~15.6 W |
| 11./12.09. | off | 285 (one transition) | ~4.3 W |

So the energised socket costs roughly 11 W, about 70 % of the idle draw.
`homeassistant/offgrid_night.yaml` switches it off between sunset and sunrise;
details in [setup.md](setup.md), section "The off-grid socket".

Two caveats on those figures. The 4.3 W rest on a single percentage point
against six for the 15.6 W, so treat the exact value as provisional. And both
runs sit at 14–15 %, where the LiFePO4 curve is flat and the BMS estimate of a
percentage point is coarse.

The discharge stop protects the battery from the controller, not from the
device itself: below it, the pack keeps draining.

**Note on DP 149 – system fault:** the app shows this value in the storage
menu labelled "system fault". A deliberately induced overload on the AC output
set it to 128; the device dropped the grid side (DP 155, 137 and 139 all went
to zero) and the value stayed at 128 for more than five minutes afterwards,
with the battery already charging again. Only a restart of the device cleared
it. Whether the datapoint is writable was never tested — the one attempt never
reached the device.

The value looks like a bit field: only 2 and 128 have been seen, both powers of
two, and 2 appears in undisturbed operation alongside 101 and 114. That is an
educated guess, not a proven fact. A combined value such as 130 would settle
it. The bridge decodes bit 7 as the AC overload and reports every other bit
honestly as unknown, with its numeric value.

**Note on DP 122:** this is the single most important value on the device. It
limits the total battery charge power, from PV as well as from the grid. Set to
0, the device shuts down its MPPT controllers and takes no solar energy at all,
even with an empty battery and full sun. See the section on quirks below.

---

## Confirmed – measurements (read-only)

| DP | Meaning | Scaling | Confidence |
|---|---|---|---|
| 102 | Battery SoC | % | certain |
| 105 | Serial number | string | certain |
| 117 | Operating mode currently in effect | enum | certain |
| 125 | PV yield today (resets at midnight) | Wh | certain |
| 126 | PV yield total | Wh | certain |
| 127 | Total pack voltage | ÷100 → V | certain |
| 128 | Battery power (negative = discharging) | W | certain |
| 130 | Highest single cell voltage | mV | certain |
| 131 | Lowest single cell voltage | mV | certain |
| 132 / 133 | Cell temperatures (highest / lowest) | ÷10 → °C | certain |
| 134 | Status: `charge_status` / `discharge_status` / `standy_status` | enum | certain |
| 135 | Off-grid output voltage, not a state readout (see note) | ÷10 → V | certain |
| 137 | Grid power (mirrors 155) | W | likely |
| 138 | Grid frequency | ÷100 → Hz | certain |
| 139 | Grid voltage | ÷10 → V | certain |
| 149 | System fault register, latched (see note) | – | certain |
| 136 | Grid current | ÷10 → 0.8 A | certain |
| 141 / 142 | Off-grid socket load, both registers identical | W | certain |
| 140 | Off-grid output current, 0 when the socket is off | ÷10 → 8.7 A | certain |
| 143 | Total PV power | W | certain |
| 155 | AC output power (**excluding** off-grid load) | W | certain |

### MPPT strings

Power, voltage and current are not contiguous in the address space:

| String | Power (W) | Voltage (÷10 → V) | Current (÷100 → A) |
|---|---|---|---|
| PV1 | 147 | 148 | 150 |
| PV2 | 164 | 178 | 179 |
| PV3 | 151 | 183 | 163 |
| PV4 | 180 | 181 | 182 |

Verified repeatedly: voltage × current = power, and the four string powers sum
to DP 143.

### Firmware and identification strings

All confirmed via the "Version" menu in the app:

| DP | Value on my unit | Meaning |
|---|---|---|
| 107 | `1.02` | Main control, hardware version |
| 108 | `1.17` | Main control, software version |
| 109 | `108` | Inverter, hardware version |
| 110 | `120` | Inverter, software version |
| 112 | `2.05` | BMS, software version |
| 113 | `1.01` | Wi-Fi module, hardware version |
| 114 | `1.01` | Wi-Fi module, software version (drops to `0.00` during reconnects) |
| 184 | `106` | Inverter, PV software version |
| 103 / 104 / 106 | masked strings | device model / device code / inverter code |

Also confirmed, each by entering a test value in the app:

| DP | Meaning |
|---|---|
| 115 | Energy meter serial number |
| 145 | OTA URL for the Espressif module (hidden field) |
| 152 | Wi-Fi name used to reach the energy meter |
| 153 | Wi-Fi password used to reach the energy meter |
| 154 | Network configuration switch (triggers meter pairing) |

The meter appears to open its own access point named after its serial number;
the device joins it rather than both sitting on the home network. I have not
been able to complete the pairing yet.

---

## Still unknown

| DP | Observed behaviour | Guess |
|---|---|---|
| 101 | bool, briefly flips to `true`, usually together with 114 and 149 | update flag? |
| 111 | constant 0 | – |
| 116 | constant 0 | – |
| 129 | counter; once ran from 0 to 13 in 20 s, then reset to 0 | grid sync timer? |

| 144 | constant 0 | – |

Also present in the app but not mapped to any datapoint: **battery status**,
**battery protection** (storage menu), and **AC output voltage** and
**current** (load menu).

---

## Quirks worth knowing

### 1. DP 122 disables PV harvesting

The label "maximum allowable battery charging power" does not suggest that the
value also governs PV charging. With DP 122 at 0 the device takes no solar
energy at all: open-circuit voltage is present at the MPPT inputs (around 37 V)
but no current flows (0.01 A). The device goes to standby because it has
nothing to do – and in standby it does not harvest.

This behaviour is fully reversible and reproducible: set DP 122 to 100 W and
the MPPT controllers start immediately, even with a nearly empty battery.

For any integration: hold DP 122 permanently above your PV power. It is an
enable, not a control variable.

### 2. DP 118 blocks export, not grid charging

Despite the name "backflow prevention", this refers to backflow *into the
grid*. Enabling it stops the device from feeding in and sends it to standby. It
has no effect on charging from the grid.

I have not found a reliable way to block grid charging while allowing PV
charging.

### 3. The off-grid socket runs on a separate path

The load on the off-grid socket (DP 141/142) is **not** included in the AC
output power (DP 155) and does not appear at the grid meter. It is therefore
invisible to any zero-export controller.

Measured example: 119 W at the AC output, 298 W off-grid load at the same
moment, battery delivering 471 W. The difference is conversion losses across
two paths.

Estimate remaining runtime from the battery power (DP 128), not from the AC
output.

### 4. 16-bit overflows in power values

Under weak irradiance DP 143 returned 65535 while the four string powers summed
to 6 W. That is −1 as a signed 16-bit integer. The same pattern appears on
DP 142.

The app displays these values unfiltered, and the device's own yield counter
(DP 125) accumulates them: on a heavily overcast morning it read 9026 Wh where
roughly 800 Wh was physically possible.

### 5. Delta frames

After the initial full status the device sends only changed datapoints.
Anything reading with `receive()` must merge frames into a cache, otherwise
half the values appear to be missing.

### 6. One local connection only

The device accepts a single local connection at a time. Opening the app
disconnects any integration running in parallel, and vice versa.

---

## Method

- **Settings:** change one value at a time in the app, then compare the full
  status against a cached copy to see which DP moved. This mapped the entire
  schedule structure without guesswork.
- **Measurements:** simultaneous comparison with the app display, plus physical
  cross-checks. The energy balance closes: 166 W PV + 660 W battery = 826 W in,
  804 W at the AC output, i.e. 97 % efficiency.
- **Cell voltages:** 51.55 V pack voltage ÷ 16 cells = 3222 mV, and DP 130/131
  read 3232 / 3225 mV. The spread widens as the pack empties, as expected for
  LiFePO4.
- **Overnight logging:** cell voltages fall slowly and monotonically,
  temperatures cool down – that is how DP 130 to 133 were identified.

Corrections and additions welcome.
