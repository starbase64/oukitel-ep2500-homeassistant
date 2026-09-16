# Configuration reference

Every setting is an environment variable in `docker-compose.yml`. All of
them have defaults; a minimal install only needs the first group.

Settings that are also editable from the dashboard are stored as retained
MQTT and survive a restart - the value here is only the fallback before
anything has been stored.

## Device and broker

| Variable | Default | Meaning |
|---|---|---|
| `EP2500_ID` | (empty) | Tuya device ID |
| `EP2500_KEY` | (empty) | Tuya local key |
| `EP2500_IP` | (empty) | device address |
| `EP2500_PORT` | `6668` | Tuya port |
| `MQTT_HOST` | `127.0.0.1` | broker address |
| `MQTT_PORT` | `1883` | broker port |
| `MQTT_USER` | (empty) | broker user, empty for none |
| `MQTT_PASS` | (empty) | broker password |

## Meter

| Variable | Default | Meaning |
|---|---|---|
| `GRID_SOURCE` | `http` | `http` (Eco Tracker endpoint, needed for the phases) or `mqtt` |
| `ECO_URL` | `http://192.168.1.100/status` | meter endpoint when `GRID_SOURCE=http` |
| `ECO_FIELD` | `power` | field in that reply, e.g. `power` or `powerAvg` |
| `ECO_INTERVAL` | `5` | seconds between meter reads |
| `ECO_SPIKE` | `600` | W; a jump larger than this waits for confirmation. 0 disables |
| `GRID_TOPIC` | `ecotracker/power` | MQTT topic when `GRID_SOURCE=mqtt` |
| `GRID_JSON_KEY` | `power` | field inside that payload |
| `GRID_FAIL_LIMIT` | `0` | W; export limit while the meter is silent |

## Limits

| Variable | Default | Meaning |
|---|---|---|
| `LIMIT_MAX` | `800` | W; export ceiling at startup. Also editable from the dashboard, where it is stored as retained MQTT |
| `LIMIT_HW_MAX` | `1500` | W; highest value that dashboard field accepts |
| `LIMIT_SAFE` | `0` | W; export limit on shutdown |
| `CHARGE_HW_MAX` | `4000` | W; ceiling on the charge limit (DP 122) |
| `BATT_WH` | `2048` | usable capacity, for the runtime estimate |
| `METER_TARGET_MIN` | `-2000` | W; lower bound for the meter target |
| `METER_TARGET_MAX` | `2000` | W; upper bound for the meter target |

## Phases and meter target

| Variable | Default | Meaning |
|---|---|---|
| `EP_PHASE` | (empty) | 1-3; which phase the device is wired to |
| `METER_TARGET_REF` | `auto` | `auto`, `sum` or `own_phase` - see Part 7.4 |
| `PHASE_SMOOTH` | `0.25` | 0-1; smoothing weight for the phase readings |
| `PHASE_LOG_INTERVAL` | `60` | seconds between phase lines in the log. 0 disables |
| `PASS_OVERRIDE` | `3` | points above the pass-through target at which protection overrides a meter target |

## Probe, dip and surplus

| Variable | Default | Meaning |
|---|---|---|
| `PROBE_SETTLE` | `90` | seconds before a probe is judged. Must outlast a neighbour's reaction |
| `PROBE_ACCEPT` | `0.6` | share of a step the meter must follow to count as genuine |
| `PROBE_MIN_DRAW` | `60` | W; below this a probe gives no verdict |
| `NEG_PROBE_STEP` | `150` | W per verified step on a negative target |
| `NEG_RETRY` | `600` | seconds a cap holds before the dip test runs again |
| `NEG_DIP` | `150` | W withheld during the dip test |
| `SURPLUS_ENTER_W` | `100` | W of surplus needed before charging is considered |
| `SURPLUS_ENTER_S` | `120` | seconds that surplus has to persist |
| `SURPLUS_EXIT_S` | `90` | seconds without surplus before charging stops |
| `SURPLUS_MIN_W` | `100` | W; below this the charge limit is not worth holding |
| `SURPLUS_STEP` | `200` | W per verified step |
| `BACKUP_POWER` | `800` | W; default for backup charging |

## Optional neighbour reading

| Variable | Default | Meaning |
|---|---|---|
| `NEIGHBOUR_TOPIC` | (empty) | optional MQTT topic with a neighbouring battery's power |
| `NEIGHBOUR_FIELD` | `bat_p` | field inside that payload |
| `NEIGHBOUR_SIGN` | `discharge_positive` | `discharge_positive` or `charge_positive` |
| `NEIGHBOUR_DISCHARGE_W` | `40` | W above which the neighbour counts as discharging |
| `NEIGHBOUR_MAX_AGE` | `60` | seconds before that reading is ignored |

## Shelly plugs

| Variable | Default | Meaning |
|---|---|---|
| `SHELLY_IP` | (empty) | plug in front of the EP2500 |
| `SHELLY2_IP` | (empty) | second plug |
| `SHELLY2_ON_OFFGRID` | `false` | `true` when that plug hangs on the off-grid outlet |
| `AC_STORAGE1_IP` | (empty) | plug in front of an AC-coupled storage unit |
| `AC_STORAGE2_IP` | (empty) | second one |
| `SHELLY_INTERVAL` | `5` | seconds between Shelly polls |
| `SHELLY_STALE_AFTER` | `5` | failed polls before the reading is dropped |

## Off-grid outlet

| Variable | Default | Meaning |
|---|---|---|
| `OFFGRID_SOC_MARGIN` | `5` | points above the discharge stop at which the outlet is usually released |
| `OFFGRID_MISMATCH_S` | `60` | seconds the outlet may read 0 A before it is reported |
| `OFFGRID_REMIND_S` | `14400` | seconds between reminders while it stays dead. 0 = once |

## Recordings and diagnostics

| Variable | Default | Meaning |
|---|---|---|
| `REC_DIR` | `/data/recordings` | where recordings are written |
| `REC_HOST` | (empty) | address shown in the download link. Must be set for the link to work |
| `REC_PORT` | `8099` | port of the download server |
| `REC_MAX_MB` | `50` | MB per recording before it stops itself |
| `REC_KEEP` | `10` | how many recordings are kept |
| `LOG_DPS` | `0` | 0 none, 1 known datapoints, 2 all |
| `TUYA_COMMAND_TIMEOUT` | `12` | seconds a command may take |
| `TUYA_RECEIVE_TIMEOUT` | `1` | seconds the receive loop waits per frame |
| `WORKER_RESTART_S` | `15` | seconds before a crashed background loop restarts |
