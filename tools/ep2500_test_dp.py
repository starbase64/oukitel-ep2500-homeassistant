#!/usr/bin/env python3
"""
Setzt einen einzelnen Datenpunkt am Oukitel EP2500, beobachtet die Wirkung
und setzt den alten Wert danach automatisch wieder zurueck.

Aufruf:
    python ep2500_test_dp.py <DP-Nummer> <Wert> [Dauer in s]

Beispiele:
    python ep2500_test_dp.py 122 300      # Ladeleistungsgrenze auf 300 W
    python ep2500_test_dp.py 121 200 30   # Einspeisegrenze, nur 30 s lang

Das Zuruecksetzen laeuft im finally-Block und greift daher auch bei
Strg+C oder einem Fehler.

WICHTIG:
  - Oukitel-App vorher schliessen (nur eine lokale Verbindung moeglich).
  - switch the bridge's control off so it does not interfere.
  - Nur DPs anfassen, deren Bedeutung bekannt ist. Netzparameter und
    Laendereinstellung liegen ebenfalls in diesem Bereich.
"""

import sys
import time
import tinytuya

# --- anpassen ---------------------------------------------------------
DEVICE_ID = "YOUR_DEVICE_ID"
DEVICE_IP = "YOUR_DEVICE_IP"
LOCAL_KEY = "YOUR_LOCAL_KEY"
DEVICE_PORT = 6668
# ----------------------------------------------------------------------

# Datapoints printed alongside every step
WATCH = {
    "128": "Akku",
    "155": "AC-Aus",
    "143": "PV",
    "134": "Status",
    "102": "SoC",
    "121": "Einsp.Grenze",
    "122": "Ladegrenze",
}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    dp = str(int(sys.argv[1]))
    val = int(sys.argv[2])
    dauer = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    d = tinytuya.OutletDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY, port=DEVICE_PORT)
    d.set_version(3.5)
    d.set_socketPersistent(True)
    d.set_socketTimeout(8)

    def snap(tag):
        s = (d.status() or {}).get("dps", {})
        teile = [f"DP{dp}={s.get(dp)}"]
        for k, name in WATCH.items():
            if k != dp:
                teile.append(f"{name}={s.get(k)}")
        print(f"{tag:>10}  " + "  ".join(teile), flush=True)
        return s

    start = snap("vorher")
    if dp not in start:
        print(f"\nWARNING: DP {dp} did not appear in the status. Aborting.")
        return
    alt = start[dp]

    changed = False
    try:
        print(f"\n-> setze DP {dp}: {alt} -> {val}")
        response = d.set_value(int(dp), val)
        print("   Antwort:", response)
        if response is None or (isinstance(response, dict)
                                and "Error" in response):
            raise RuntimeError(f"Schreibbefehl abgelehnt: {response}")
        changed = True
        print()

        vergangen = 0
        while vergangen < dauer:
            time.sleep(10)
            vergangen += 10
            snap(f"nach {vergangen}s")
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer.")
    finally:
        if changed:
            print(f"\n-> setze DP {dp} zurueck auf {alt}")
            for versuch in range(3):
                d.set_value(int(dp), alt)
                time.sleep(5)
                ist = (d.status() or {}).get("dps", {}).get(dp)
                if ist == alt:
                    print(f"   bestaetigt: DP {dp} = {ist}")
                    break
                print(f"   Versuch {versuch + 1}: DP {dp} = {ist}, wiederhole")
            else:
                print(f"   CAUTION: restore not confirmed. "
                      f"DP {dp} manuell auf {alt} pruefen!")
            snap("Ende")


if __name__ == "__main__":
    main()
