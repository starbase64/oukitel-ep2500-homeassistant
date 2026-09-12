# Oukitel EP2500 – local control for Home Assistant

Full local integration for the Oukitel EP2500 balcony energy storage system,
including a zero-export controller. After the one-time Tuya provisioning, no
cloud or vendor app is required for operation.

The EP2500 has **no data model registered in the Tuya cloud**, so no
off-the-shelf integration works with it. This project documents the local
datapoints and provides a bridge that exposes them to Home Assistant over MQTT.

Tested against firmware 1.02 / 1.17 with an Eco Tracker as the grid meter.
Regulation holds the grid connection at ±4 W.

## What you get

- 30+ sensors: SoC, all four MPPT strings individually, cell voltages,
  temperatures, power values, off-grid load, voltage and current
- Feed-in limit, battery charge limit, PV charge limit and SoC thresholds
  continuously adjustable
- Zero-export controller with anti-windup and idle detection
- Configurable meter target: `0 W` for zero export, a positive value for
  deliberate grid import, or a negative value for deliberate export — for
  example to make another AC-coupled storage unit charge before the EP2500 is
  full
- Two optional Shelly inputs for AC-coupled storage units. Their measured
  charging power is folded into a negative meter target, preventing the
  controller from ramping up again as the other storage absorbs the surplus
- **Pass-through mode**: above a configurable state of charge the controller
  stops regulating against the meter. It pins the export limit at `LIMIT_MAX`
  and trims the PV charge power so the state of charge holds steady, keeping
  the device away from the charge stop where it would shut the solar side down
- **System fault sensor**: DP 149 is decoded and surfaced in Home Assistant.
  The register latches — it survives until the device is restarted, so without
  a sensor a fault sits there unnoticed for days
- Optional Home Assistant automation that switches the off-grid socket off
  overnight. The device keeps the socket energised at idle, and standby draw
  measured at about 11 W for the socket alone — see "The off-grid socket" in
  [docs/setup.md](docs/setup.md), YAML in `homeassistant/offgrid_night.yaml`
- **Log recording from the dashboard**: a switch starts mirroring the bridge's
  log into a file, and the bridge serves the recordings over HTTP so you can
  download them without shell access
- **Signed meter target with AC-storage compensation**: aim for a deliberate
  export so a second, AC-coupled storage unit starts charging. Shelly plugs in
  front of those units measure what they absorb, and that is deducted from the
  request, so the two controllers cannot ramp each other up
- Estimated remaining runtime
- Controller parameters adjustable from the dashboard, no restart needed
- Event log covering the last 48 hours
- Plausibility filter for the firmware's 16-bit overflow values
- Meter readings are filtered the same way whichever source they come from:
  absurd values dropped, large jumps held back until the next reading confirms
  them
- Optional Shelly integration for independent cross-checks

## Documentation

- [Datapoint reference](docs/datapoints.md) – all 83 DPs, what is known and
  what is not
- [Setup guide](docs/setup.md) – from Tuya developer account to running
  controller

## Quick start

You need the device ID, local key and IP address of your EP2500. See the
[setup guide](docs/setup.md) for how to obtain them.

```bash
git clone https://github.com/starbase64/oukitel-ep2500-homeassistant.git
cd oukitel-ep2500-homeassistant
cp docker-compose.example.yml docker-compose.yml
# edit docker-compose.yml with your values
docker compose up -d --build
docker logs -f ep2500-bridge
```

The entities appear in Home Assistant automatically via MQTT discovery. No HA
restart needed.

For the dashboard, create a new dashboard, open the raw configuration editor
and paste [`dashboard.yaml`](dashboard.yaml).

## Read this before anything else

**DP 122 limits the total battery charge power – from PV as well as from the
grid.** Set to 0, the device shuts down its MPPT controllers and takes no solar
energy at all, even with an empty battery and full sun. The bridge holds this
value open automatically; if you use your own integration, do the same.

**DP 118 ("backflow prevention") blocks export, not grid charging.** Enabling
it stops the device from feeding in and sends it to standby.

**There is no direct PV-to-grid path.** The device only runs its MPPT
controllers while it can charge the battery. Once the charge-stop SoC is
reached it shuts the solar side down, discharges to serve the export, and
restarts the PV about five percent lower. The pass-through mode in this bridge
works around that by holding the state of charge steady below the charge stop.

All three cost me days of troubleshooting. Details in the
[datapoint reference](docs/datapoints.md).

## Caveats

- The device accepts only **one local connection** at a time. Opening the
  vendor app disconnects the bridge.
- The 800 W feed-in cap is a software limit. The device accepts higher values
  without complaint. `LIMIT_MAX` in the configuration is what actually caps it.
- Writing to undocumented datapoints of a grid-tied inverter is at your own
  risk. Grid parameters and country settings live in the same address space.
- Multiple plug-in solar devices at one grid connection point are counted
  together in Germany. Different phases do not multiply the allowance.

## Contributing

Corrections to the datapoint mapping are very welcome, especially for the ones
still marked unknown. If you own an EP2500 and can confirm or correct something,
please open an issue.

The bridge publishes English entity names. Existing installations upgraded
from an older German-language release keep their entity IDs; see below.

## License

MIT – see [LICENSE](LICENSE).

Not affiliated with Oukitel or Shenzhen Yunji New Energy Technology.

## Upgrading from a German-language version

Everything the bridge publishes is now in English: entity names, log lines and
events. If you ran an earlier build, be aware of what that does and does not
change in Home Assistant.

Entities are matched by `unique_id`, which did not change. HA therefore keeps
your existing **entity IDs** and only updates the friendly names. A sensor that
was `sensor.oukitel_ep2500_ladestand` stays `sensor.oukitel_ep2500_ladestand`
and merely displays as "State of charge". Your history and statistics survive.

`dashboard.yaml` in this repository uses the **new** English entity IDs,
because that is what a fresh install produces. On an upgraded instance it will
therefore show unavailable rows. Two ways out:

- **Recommended:** generate a dashboard for the entity IDs already stored in
  your HA registry. This preserves names, history and statistics:

  ```bash
  cd /path/to/oukitel-ep2500-homeassistant
  python3 tools/fix_dashboard_ids.py \
    /path/to/homeassistant/config \
    dashboard.yaml dashboard.local.yaml
  ```

  The second argument is the directory holding your `configuration.yaml`; the
  tool reads `.storage/core.entity_registry` below it. Prefix with `sudo` if
  that directory belongs to the container user.

  Restart the bridge first if the tool reports missing MQTT entities. Then
  paste `dashboard.local.yaml` into the dashboard's raw configuration editor,
  or copy it into HA's dashboard directory.
- Alternatively, rename every entity under Settings > Devices & Services >
  Entities so it matches the new scheme and use `dashboard.yaml` unchanged.
  Renaming an entity in HA preserves its history.

The repair tool resolves every dashboard reference through the stable MQTT
`unique_id`. It also reports missing entities, unknown dashboard references and
duplicate MQTT unique IDs instead of silently producing a partly broken view.

The helper and the automations for the off-grid night switch carry English
names too. If you created them from an earlier build, the helper is now
`input_boolean.ep2500_offgrid_night_off` and the automation IDs are
`ep2500_offgrid_sunset`, `ep2500_offgrid_sunrise`, `ep2500_offgrid_apply_now`
and `ep2500_offgrid_helper_off`. Those files are yours, not the bridge's, so
rename them by hand — or leave them as they are and skip
`homeassistant/offgrid_night.yaml`, since nothing else refers to those IDs.
