#!/usr/bin/env python3
"""Rewrites the English dashboard onto the entity IDs this install actually uses.

Home Assistant keeps an entity_id once it has been assigned. On an install that
started out with German entity names, renaming them in the bridge changes only
the friendly names - sensor.oukitel_ep2500_ladestand stays what it is. The
dashboard shipped with the project uses the English IDs a fresh install gets,
so it finds nothing here.

This script resolves each reference through the unique_id, which never changed,
and writes a dashboard that matches the local registry.

Usage:
    python3 fix_dashboard_ids.py <ha-config-dir> <dashboard-in> <dashboard-out>
"""
import json
import os
import re
import sys

# English entity_id (as used in the shipped dashboard) -> unique_id.
#
# Keep this list in sync with dashboard.yaml and publish_discovery().  The
# unique IDs are stable across bridge versions whereas Home Assistant keeps
# the entity IDs that were assigned by the first (for example German) build.
UID = {
    "binary_sensor.oukitel_ep2500_fault": "ep2500_fault_active",
    "binary_sensor.oukitel_ep2500_pass_through_running": "ep2500_pass_active",
    "number.oukitel_ep2500_battery_charge_limit": "ep2500_charge",
    "number.oukitel_ep2500_charge_stop": "ep2500_socmax",
    "number.oukitel_ep2500_control_interval": "ep2500_tune_interval",
    "number.oukitel_ep2500_controller_gain": "ep2500_tune_gain",
    "number.oukitel_ep2500_deadband": "ep2500_tune_deadband",
    "number.oukitel_ep2500_discharge_stop": "ep2500_socmin",
    "number.oukitel_ep2500_export_limit": "ep2500_limit",
    "number.oukitel_ep2500_max_step": "ep2500_tune_max_step",
    "number.oukitel_ep2500_meter_target": "ep2500_correction",
    "number.oukitel_ep2500_pass_through_from_soc": "ep2500_tune_soc_pass",
    "number.oukitel_ep2500_pv_limit_when_open": "ep2500_tune_pv_max",
    "sensor.oukitel_ep2500_ac_storage_1_charging_power": "ep2500_ac_storage1_power",
    "sensor.oukitel_ep2500_ac_storage_2_charging_power": "ep2500_ac_storage2_power",
    "sensor.oukitel_ep2500_ac_storage_charging_power_total": "ep2500_ac_storage_charge_power",
    "sensor.oukitel_ep2500_ac_output_power": "ep2500_ac_out",
    "sensor.oukitel_ep2500_battery_power": "ep2500_batt_power",
    "sensor.oukitel_ep2500_battery_voltage": "ep2500_batt_voltage",
    "sensor.oukitel_ep2500_cell_voltage_max": "ep2500_cell_max",
    "sensor.oukitel_ep2500_cell_voltage_min": "ep2500_cell_min",
    "sensor.oukitel_ep2500_controller_setpoint": "ep2500_soll",
    "sensor.oukitel_ep2500_effective_meter_target": "ep2500_meter_target_effective",
    "sensor.oukitel_ep2500_events": "ep2500_events",
    "sensor.oukitel_ep2500_grid_current": "ep2500_grid_current",
    "sensor.oukitel_ep2500_grid_frequency": "ep2500_grid_freq",
    "sensor.oukitel_ep2500_grid_power": "ep2500_grid_power",
    "sensor.oukitel_ep2500_grid_voltage": "ep2500_grid_voltage",
    "sensor.oukitel_ep2500_meter_power_controller": "ep2500_grid",
    "sensor.oukitel_ep2500_north_pv_power": "ep2500_shelly2",
    "sensor.oukitel_ep2500_off_grid_current": "ep2500_offgrid_curr",
    "sensor.oukitel_ep2500_off_grid_load": "ep2500_offgrid_power",
    "sensor.oukitel_ep2500_off_grid_voltage": "ep2500_offgrid_volt",
    "sensor.oukitel_ep2500_operating_mode": "ep2500_mode",
    "sensor.oukitel_ep2500_pv1_current": "ep2500_pv1_current",
    "sensor.oukitel_ep2500_pv1_power": "ep2500_pv1_power",
    "sensor.oukitel_ep2500_pv1_voltage": "ep2500_pv1_voltage",
    "sensor.oukitel_ep2500_pv2_current": "ep2500_pv2_current",
    "sensor.oukitel_ep2500_pv2_power": "ep2500_pv2_power",
    "sensor.oukitel_ep2500_pv2_voltage": "ep2500_pv2_voltage",
    "sensor.oukitel_ep2500_pv3_current": "ep2500_pv3_current",
    "sensor.oukitel_ep2500_pv3_power": "ep2500_pv3_power",
    "sensor.oukitel_ep2500_pv3_voltage": "ep2500_pv3_voltage",
    "sensor.oukitel_ep2500_pv4_current": "ep2500_pv4_current",
    "sensor.oukitel_ep2500_pv4_power": "ep2500_pv4_power",
    "sensor.oukitel_ep2500_pv4_voltage": "ep2500_pv4_voltage",
    "sensor.oukitel_ep2500_pv_charge_limit": "ep2500_pv_limit",
    "sensor.oukitel_ep2500_pv_energy_today": "ep2500_pv_energy",
    "sensor.oukitel_ep2500_pv_energy_total": "ep2500_pv_energy_all",
    "sensor.oukitel_ep2500_pv_power": "ep2500_pv_power",
    "sensor.oukitel_ep2500_recording": "ep2500_record_info",
    "sensor.oukitel_ep2500_remaining_runtime": "ep2500_runtime",
    "sensor.oukitel_ep2500_shelly_power": "ep2500_shelly",
    "sensor.oukitel_ep2500_state_of_charge": "ep2500_soc",
    "sensor.oukitel_ep2500_status": "ep2500_status",
    "sensor.oukitel_ep2500_system_fault": "ep2500_fault",
    "sensor.oukitel_ep2500_temperature_1": "ep2500_temp1",
    "sensor.oukitel_ep2500_temperature_2": "ep2500_temp2",
    "switch.oukitel_ep2500_backflow_prevention": "ep2500_backflow",
    "switch.oukitel_ep2500_ac_storage_1": "ep2500_ac_storage1_switch",
    "switch.oukitel_ep2500_ac_storage_2": "ep2500_ac_storage2_switch",
    "switch.oukitel_ep2500_north_pv": "ep2500_shelly2_switch",
    "switch.oukitel_ep2500_off_grid_socket": "ep2500_offgrid",
    "switch.oukitel_ep2500_pass_through_when_battery_full": "ep2500_passthrough",
    "switch.oukitel_ep2500_record_log": "ep2500_record",
    "switch.oukitel_ep2500_shelly_mains_disconnect": "ep2500_shelly_switch",
    "switch.oukitel_ep2500_zero_export_control": "ep2500_control",
    "text.oukitel_ep2500_north_pv_shelly_ip": "ep2500_shelly2_ip",
    "text.oukitel_ep2500_shelly_ip": "ep2500_shelly_ip",
    "sensor.oukitel_ep2500_phase_1": "ep2500_phase1",
    "sensor.oukitel_ep2500_phase_2": "ep2500_phase2",
    "sensor.oukitel_ep2500_phase_3": "ep2500_phase3",
    "sensor.oukitel_ep2500_other_load_on_own_phase": "ep2500_own_phase_load",
    "select.oukitel_ep2500_ep2500_phase": "ep2500_ep_phase",
    "switch.oukitel_ep2500_backup_charge": "ep2500_backup",
    "number.oukitel_ep2500_backup_charge_power": "ep2500_backup_power",
    "switch.oukitel_ep2500_ac_surplus_charging": "ep2500_surplus",
    "binary_sensor.oukitel_ep2500_taking_surplus": "ep2500_surplus_active",
    "text.oukitel_ep2500_ac_storage_1_shelly_ip": "ep2500_ac_storage1_ip",
    "text.oukitel_ep2500_ac_storage_2_shelly_ip": "ep2500_ac_storage2_ip",
}

ENTITY_REF = re.compile(
    r"(?<![\w.])((?:binary_sensor|number|select|sensor|switch|text)\."
    r"oukitel_ep2500_[a-z0-9_]+)(?![\w])"
)


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    config_dir, src, dst = sys.argv[1:]

    registry = os.path.join(config_dir, ".storage", "core.entity_registry")
    with open(registry, encoding="utf-8") as fh:
        data = json.load(fh)

    # unique_id -> entity_id, restricted to this integration
    local = {}
    duplicate_uids = {}
    for entry in data["data"]["entities"]:
        if entry.get("platform") == "mqtt" and entry.get("unique_id"):
            uid = entry["unique_id"]
            entity_id = entry["entity_id"]
            if uid in local and local[uid] != entity_id:
                duplicate_uids.setdefault(uid, [local[uid]]).append(entity_id)
            local[uid] = entity_id

    text = open(src, encoding="utf-8").read()
    replaced, missing, unchanged = 0, [], 0

    for english_id, unique_id in sorted(UID.items(), key=lambda kv: -len(kv[0])):
        if english_id not in text:
            continue
        actual = local.get(unique_id)
        if actual is None:
            missing.append((english_id, unique_id))
            continue
        if actual == english_id:
            unchanged += 1
            continue
        text = re.sub(rf"(?<![\w.]){re.escape(english_id)}(?![\w])", actual, text)
        replaced += 1
        print(f"  {english_id}\n    -> {actual}")

    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)

    unresolved = sorted({
        entity_id for entity_id in ENTITY_REF.findall(text)
        if entity_id in UID and UID[entity_id] not in local
    })

    unknown_refs = sorted({
        entity_id for entity_id in ENTITY_REF.findall(text)
        if entity_id not in UID and entity_id not in set(local.values())
    })

    print(f"\n{replaced} rewritten, {unchanged} already correct, "
          f"{len(missing)} not found in the registry")
    for english_id, unique_id in missing:
        print(f"  missing: {english_id} (unique_id {unique_id})")
    if unresolved:
        print("\nDashboard entities not published in the local MQTT registry:")
        for entity_id in unresolved:
            print(f"  {entity_id} (unique_id {UID[entity_id]})")
    if unknown_refs:
        print("\nDashboard references unknown to this tool and the local registry:")
        for entity_id in unknown_refs:
            print(f"  {entity_id}")
    if duplicate_uids:
        print("\nWARNING: duplicate MQTT unique IDs in the entity registry:")
        for uid, entity_ids in sorted(duplicate_uids.items()):
            print(f"  {uid}: {', '.join(entity_ids)}")
    print(f"\nwritten: {dst}")
    return 2 if unresolved or unknown_refs or duplicate_uids else 0


if __name__ == "__main__":
    sys.exit(main())
