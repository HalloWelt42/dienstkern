"""Die Vorlage eines verwalteten Dienstes: was gestartet wird, wie man sieht, dass er läuft, und wie er angehalten wird.

Programme, die auf dem Kern aufbauen, erweitern die Vorlage um ihre eigenen Felder (Unterklasse) und lesen sie aus
ihren eigenen Dateien. Unbekannte Felder sind ein Fehler, nie stillschweigend übergangen: ein Tippfehler in einer
Datei soll auffallen.
"""

from __future__ import annotations

import os
import re
import tomllib
from enum import Enum
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

KENNUNG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}(\.[a-z0-9][a-z0-9_-]{0,63})?$")
STANDARD_PFAD = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def pfad(wert: str) -> Path:
    """~ und $NAME aus der Umgebung des Programms; für Ordner in Dateien."""
    return Path(os.path.expandvars(wert)).expanduser()


class Steuerung(str, Enum):
    PROZESS = "prozess"    # der Befehl ist der Dienst: er läuft, bis er angehalten wird (eigene Prozessgruppe)
    BEFEHLE = "befehle"    # der Befehl startet etwas und kehrt zurück (zum Beispiel Container); Prüfen und Anhalten per Befehl


class Vorlage(BaseModel):
    """Ein Dienst läuft in eigener Sitzung (Prozessgruppe), eigenem Arbeitsordner, mit eigener Umgebung (nur Pfad,
    Heimordner, Sprache und was hier steht) und eigener Protokolldatei."""

    model_config = ConfigDict(extra="forbid")

    kennung: str
    name: str
    ordner: Path
    befehl: list[str] = Field(min_length=1)
    vorher: list[str] = Field(default_factory=list, description="Befehl, der vor jedem Start einmal läuft (zum Beispiel Härtung)")
    port: int | None = Field(default=None, ge=1, le=65535)
    pruefadresse: str | None = Field(default=None, description="erreichbar heißt: Antwort unter 500; sonst reicht der offene Port")
    oberflaeche: str | None = Field(default=None, description="Adresse einer eigenen Oberfläche des Dienstes, zum Öffnen")
    beschreibung: str = ""
    speicher_gb: float = Field(default=1.0, ge=0, description="Schätzung bis zur ersten Messung")
    startzeit_s: float = Field(default=120, gt=0, description="so lange darf der Start bis zur Erreichbarkeit dauern")
    anhaltezeit_s: float = Field(default=20, gt=0, description="so lange darf das Beenden dauern, danach hart")
    umgebung: dict[str, str] = Field(default_factory=dict)
    umgebung_datei: Path | None = Field(default=None, description="Datei mit Zeilen NAME=WERT, zum Beispiel die .env des Projekts")
    pfad: str = STANDARD_PFAD
    neustart: bool = Field(default=True, description="nach einem Absturz neu starten, solange er laufen soll")
    steuerung: Steuerung = Field(default=Steuerung.PROZESS)
    pruefbefehl: list[str] = Field(default_factory=list, description="erreichbar heißt: dieser Befehl endet mit 0")
    anhaltebefehl: list[str] = Field(default_factory=list, description="hält ihn an; dann nie per Signal (Pflicht bei steuerung = befehle)")
    prozessmuster: str | None = Field(default=None, description="regulärer Ausdruck auf die Befehlszeile der Prozesse, deren Speicher zählt (für befehle)")
    braucht: list[str] = Field(default_factory=list, description="Kennungen von Diensten, die vor diesem laufen müssen")
    aktiv_ab_prozent: float = Field(default=5.0, ge=0, description="ab dieser Prozessorlast (Prozent eines Kerns) gilt er als arbeitend")

    @field_validator("ordner", "umgebung_datei", mode="before")
    @classmethod
    def _aufloesen(cls, wert: object) -> object:
        return pfad(wert) if isinstance(wert, str) else wert

    @field_validator("kennung")
    @classmethod
    def _kennung(cls, wert: str) -> str:
        if not KENNUNG.match(wert):
            raise ValueError("Kennung nur aus Kleinbuchstaben, Ziffern, - und _")
        return wert

    @model_validator(mode="after")
    def _steuerung_vollstaendig(self) -> Vorlage:
        if self.steuerung == Steuerung.BEFEHLE:
            if not self.anhaltebefehl:
                raise ValueError("steuerung = befehle braucht einen anhaltebefehl")
            if not (self.port or self.pruefadresse or self.pruefbefehl):
                raise ValueError("steuerung = befehle braucht port, pruefadresse oder pruefbefehl, um zu sehen, ob er läuft")
        if self.prozessmuster:
            re.compile(self.prozessmuster)
        return self

    def programm(self, umgebung: dict[str, str]) -> list[str]:
        """Befehl mit aufgelösten Variablen aus der Umgebung des Dienstes; ein relatives Programm gilt ab `ordner`."""
        teile = [erweitern(t, umgebung) for t in self.befehl]
        if "/" in teile[0] and not os.path.isabs(teile[0]):
            teile[0] = str(self.ordner / teile[0])
        return teile


class Ladefehler(BaseModel):
    datei: str
    meldung: str


V = TypeVar("V", bound=BaseModel)


def lesbar(e: ValidationError) -> str:
    """Feld und Grund statt der Sammelzeile "1 validation error"."""
    teile = []
    for f in e.errors()[:3]:
        feld = ".".join(str(x) for x in f["loc"])
        grund = str(f["msg"]).removeprefix("Value error, ")
        if f["type"] == "extra_forbidden":
            grund = "unbekanntes Feld"
        teile.append(f"{feld}: {grund}" if feld else grund)
    return "; ".join(teile)[:300]


def dateien_lesen(ordner: Path, typ: type[V]) -> tuple[list[V], list[Ladefehler]]:
    """Alle `*.toml` eines Ordners als `typ`, der Dateiname ist die Kennung. Fehlerhafte werden gemeldet und
    ausgelassen, nie geraten."""
    gelesen, fehler = [], []
    for datei in sorted(ordner.glob("*.toml")):
        try:
            with open(datei, "rb") as f:
                gelesen.append(typ.model_validate({**tomllib.load(f), "kennung": datei.stem}))
        except ValidationError as e:
            fehler.append(Ladefehler(datei=datei.name, meldung=lesbar(e)))
        except (OSError, tomllib.TOMLDecodeError) as e:
            fehler.append(Ladefehler(datei=datei.name, meldung=str(e).splitlines()[0][:200]))
    return gelesen, fehler


def erweitern(text: str, umgebung: dict[str, str]) -> str:
    """~ am Anfang und $NAME oder ${NAME} aus der gegebenen Umgebung (nicht der des Programms); Unbekanntes bleibt stehen."""
    text = re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda m: umgebung.get(m.group(1) or m.group(2), m.group(0)), text)
    return str(Path(text).expanduser()) if text.startswith("~") else text


def umgebung_lesen(datei: Path) -> dict[str, str]:
    """Zeilen NAME=WERT, Kommentare mit #, Werte ohne umschließende Anführungszeichen."""
    werte: dict[str, str] = {}
    for zeile in datei.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        name, _, wert = zeile.partition("=")
        name = name.removeprefix("export ").strip()
        wert = wert.strip()
        if len(wert) >= 2 and wert[0] == wert[-1] and wert[0] in "\"'":
            wert = wert[1:-1]
        werte[name] = wert
    return werte
