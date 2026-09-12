#!/usr/bin/env python3
"""
Wertet DP 140 aus dem Bridge-Log aus.

Fuehrt den Zustand aller Datenpunkte ueber die Zeit mit und zeigt bei jedem
Wechsel von DP 140, wie die uebrigen Werte in diesem Moment aussahen. Daraus
laesst sich ablesen, welche Bedingung welchen Wert oder welches Bit setzt.

Aufruf auf dem Rechner, auf dem die Bridge laeuft:

    docker logs --since 24h ep2500-bridge > /tmp/ep.log
    python3 dp140_auswerten.py /tmp/ep.log

Fuer aussagekraeftige Ergebnisse sollte LOG_DPS auf 2 stehen.
"""

import collections
import re
import sys

# Datapoints printed at the transition
FELDER = [
    ("134", "Status"),
    ("119", "OG-Dose"),
    ("141", "OG-Last"),
    ("143", "PV"),
    ("155", "AC-Aus"),
    ("128", "Akku"),
    ("139", "Netz-U"),
    ("121", "Einsp"),
    ("122", "Ladegr"),
    ("118", "Rueckfl"),
]

ZEILE = re.compile(
    r"^(\d\d:\d\d:\d\d) INFO DP (\d+) (.+?) (\S+) -> (\S+)(?: \(.*\))?$")


def kurz(v):
    if v is None:
        return "?"
    v = str(v).strip("'\"")
    return v.replace("_status", "")[:12]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    zustand = {}
    uebergaenge = []

    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        for roh in f:
            zeile = " ".join(roh.split())
            m = ZEILE.match(zeile)
            if not m:
                continue
            zeit, dp, _name, _alt, neu = m.groups()
            if dp == "140":
                uebergaenge.append((zeit, _alt, neu, dict(zustand)))
            zustand[dp] = neu

    if not uebergaenge:
        print("Keine Uebergaenge von DP 140 im Log gefunden.")
        print("Steht LOG_DPS auf 2? Und deckt der Zeitraum die Tests ab?")
        return

    print(f"{len(uebergaenge)} Uebergaenge von DP 140\n")

    haeufig = collections.Counter(n for _, _, n, _ in uebergaenge)
    print("Beobachtete Werte:")
    for wert, count in sorted(haeufig.items(),
                               key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        bits = f"{int(wert):04b}" if wert.isdigit() else "?"
        print(f"   {wert:>3}  = binaer {bits}   ({count}x)")
    print()

    kopf = f"{'Zeit':<9}{'von':>4}{'nach':>6}  " + "  ".join(
        f"{n:<12}" for _, n in FELDER)
    print(kopf)
    print("-" * len(kopf))
    for zeit, alt, neu, z in uebergaenge:
        werte = "  ".join(f"{kurz(z.get(dp)):<12}" for dp, _ in FELDER)
        print(f"{zeit:<9}{alt:>4}{neu:>6}  {werte}")

    # Auffaelligkeiten: welcher Wert tritt bei welcher Bedingung auf
    print("\n\nZusammenfassung je Zielwert:")
    gruppen = collections.defaultdict(list)
    for zeit, alt, neu, z in uebergaenge:
        gruppen[neu].append(z)

    for wert in sorted(gruppen, key=lambda x: int(x) if x.isdigit() else 999):
        entries = gruppen[wert]
        print(f"\n  DP 140 = {wert}  ({len(entries)}x)")
        for dp, name in FELDER:
            vorkommen = {kurz(z.get(dp)) for z in entries}
            if len(vorkommen) == 1:
                print(f"     {name:<10} immer {vorkommen.pop()}")
            else:
                print(f"     {name:<10} wechselnd: "
                      + ", ".join(sorted(vorkommen)[:5]))


if __name__ == "__main__":
    main()
