"""Zustände eines Dienstes und der Stand, der Neustarts übersteht (eine JSON-Datei je Programm).

Sollzustand und Prozessgruppe stehen in der Datei: nach einem Neustart des Programms oder des Rechners startet es,
was laufen soll, und erkennt eigene Gruppen wieder (nur innerhalb derselben Systemlaufzeit).
"""

from __future__ import annotations

import re
import subprocess
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from .messen import MAC


class Zustand(str, Enum):
    AUS = "aus"
    STARTET = "startet"
    LAEUFT = "laeuft"
    STOPPT = "stoppt"
    GESTOERT = "gestoert"


class Soll(str, Enum):
    LAEUFT = "laeuft"
    AUS = "aus"


class Sperre(BaseModel):
    """Ein Dienst, den die Speicherwache beendet hat: er startet nicht wieder, weder nach Sollzustand noch auf Befehl,
    bis ein Mensch die Sperre aufhebt."""

    grund: str                           # ganzer Satz, so wie Oberfläche und Leitstelle ihn zeigen
    seit: str                            # Zeitpunkt (ISO, Ortszeit)
    anlass: str = "grenze"               # grenze: über der eigenen Höchstgrenze; knapp: der Rechner wurde knapp
    gb: float | None = None              # gemessen beim Eingriff
    grenze_gb: float | None = None


class DienstStand(BaseModel):
    soll: Soll | None = None             # None: hier wurde nie etwas entschieden, der Dienst wird nur übernommen
    arbeitsspitzen: list[float] = Field(default_factory=list)   # Speicherspitze je Arbeitsphase, die jüngsten zuletzt
    gruppe: int | None = None
    sperre: Sperre | None = None         # von der Speicherwache gesetzt; hebt nur ein Mensch auf


class Gesamtstand(BaseModel):
    systemstart: int | None = None
    dienste: dict[str, DienstStand] = Field(default_factory=dict)


def systemstart() -> int | None:
    """Startzeitpunkt des Rechners in Sekunden; schützt davor, eine alte Prozessgruppe nach Neustart wiederzuerkennen."""
    try:
        if MAC:
            ausgabe = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
            return int(re.search(r"sec = (\d+)", ausgabe).group(1))
        for zeile in Path("/proc/stat").read_text().splitlines():
            if zeile.startswith("btime "):
                return int(zeile.split()[1])
    except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
        pass
    return None


# macOS verschiebt kern.boottime bei jeder Uhrkorrektur um Sekunden (gemessen 27.10.2026: 2 s); ein Neustart liegt
# immer weit mehr als diesen Spielraum vom gespeicherten Wert entfernt
SYSTEMSTART_SPIELRAUM_S = 300


def derselbe_systemstart(gespeichert: int | None, jetzt: int | None) -> bool:
    if gespeichert is None or jetzt is None:
        return gespeichert == jetzt
    return abs(gespeichert - jetzt) <= SYSTEMSTART_SPIELRAUM_S


def stand_lesen(datei: Path) -> Gesamtstand:
    jetzt = systemstart()
    try:
        stand = Gesamtstand.model_validate_json(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stand = Gesamtstand()
    if not derselbe_systemstart(stand.systemstart, jetzt):   # Rechner neu gestartet: alte Gruppen gelten nicht mehr
        for d in stand.dienste.values():
            d.gruppe = None
    stand.systemstart = jetzt                           # nachgeführt, damit sich kleine Verschiebungen nicht aufsummieren
    return stand


def stand_speichern(datei: Path, stand: Gesamtstand) -> None:
    neu = datei.with_suffix(".json.neu")
    neu.write_text(stand.model_dump_json(indent=2), encoding="utf-8")
    neu.replace(datei)
