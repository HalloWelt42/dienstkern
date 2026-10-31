"""Speicherwache: eine harte Grenze je Dienst und ein Notaus, wenn der Rechner knapp wird.

Anlass (HELF-85, 31.10.2026): ein Hördienst, angemeldet mit 6 GB, wuchs bei der ersten echten Arbeit auf über 100 GB, und
der Rechner stürzte ab. Gemessen war das (die Arbeitsspitze stand danach im Stand), aber nichts griff ein: der angemeldete
Speicher diente nur der Planung.

Die Wache misst in eigenem, kurzem Takt (Vorgabe jede Sekunde) den festen Speicher jedes laufenden Dienstes, also den
physischen Fußabdruck aller seiner Prozesse (auf dem Mac samt Grafikspeicher; RSS zählte geteilte Seiten falsch), und
greift ein, wenn

- ein Dienst über seiner Höchstgrenze liegt (`speicher_grenze_gb` der Vorlage, sonst `speicher_gb` mal `faktor` plus
  `zuschlag_gb`), oder
- der Rechner knapp wird (freier Anteil unter `frei_mindestens_prozent`, kritischer Speicherdruck des Systems, oder
  Auslagerung: über `swap_hoechstens_gb`, schneller wachsend als `swap_zuwachs_gb_je_min`, oder länger als
  `swap_geduldet_s` über `swap_geduldet_gb`; kurz und wenig wird geduldet): dann
  den Dienst, der am weitesten über seinem angemeldeten Speicher liegt (mindestens `ueberschuss_mindestens_gb`). Wer
  im Rahmen seiner Anmeldung bleibt, wird nie beendet; das Entladen von Modellen entscheidet die Leitstelle.

Eingreifen heißt: der Dienst wird gesperrt (Grund im Stand, übersteht Neustarts), sein Sollzustand ist aus, dann wird er
sofort hart beendet. Er startet nicht wieder, weder nach Sollzustand noch nach Absturzregeln noch auf Befehl von außen,
bis ein Mensch die Sperre aufhebt. Vor dem Beenden bekommt das Programm Gelegenheit, es weiterzusagen (`vor_eingriff`),
damit Anbindungen den Grund kennen, bevor die Verbindung zum Dienst abreißt.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .messen import GIB, Druckstufe, Prozesstabelle, Systemspeicher, prozess_bytes, systemspeicher
from .vorlage import Vorlage
from .zustand import Zustand

if TYPE_CHECKING:
    from .dienst import Dienst
    from .verwalter import Verwalter

log = logging.getLogger("dienstkern")


class Speicherschutz(BaseModel):
    """Einstellungen der Speicherwache."""

    aktiv: bool = Field(default=True, description="Dienste über ihrer Höchstgrenze oder bei knappem Rechner sofort beenden und sperren")
    takt_s: float = Field(default=1.0, ge=0.2, le=10, description="so oft wird gemessen, solange ein Dienst läuft")
    faktor: float = Field(default=1.5, ge=1, le=20, description="Höchstgrenze ohne eigene Angabe: speicher_gb mal diesen Faktor ...")
    zuschlag_gb: float = Field(default=4.0, ge=0, le=512, description="... plus diesen Zuschlag")
    frei_mindestens_prozent: float = Field(default=8.0, ge=0, le=50, description="darunter (freier Anteil, wie das System ihn "
                                           "nennt) gilt der Rechner als knapp; 0 schaltet diese Prüfung ab")
    druck_kritisch: bool = Field(default=True, description="kritischer Speicherdruck des Systems gilt ebenfalls als knapp")
    ueberschuss_mindestens_gb: float = Field(default=1.0, ge=0, le=512, description="ist der Rechner knapp, wird nur ein Dienst "
                                             "beendet, der mindestens so weit über seinem angemeldeten Speicher liegt")
    # Auslagerung: kurz und wenig ist nicht schlimm, dauerhaft, schnell wachsend oder viel schon (Nutzer 31.10.2026)
    swap_hoechstens_gb: float = Field(default=24.0, ge=0, le=4096, description="mehr Auslagerung gilt als knapp; 0 schaltet ab")
    swap_geduldet_gb: float = Field(default=4.0, ge=0, le=4096, description="so viel Auslagerung wird geduldet ...")
    swap_geduldet_s: float = Field(default=300, ge=10, le=86400, description="... höchstens so lange am Stück (bei Speicherdruck)")
    swap_dauer_nur_bei_druck: bool = Field(default=True, description="dauerhafte Auslagerung zählt nur, solange das System dabei "
                                           "Speicherdruck meldet (alte, ruhende Auslagerung baut macOS nur langsam ab)")
    swap_zuwachs_gb_je_min: float = Field(default=4.0, ge=0, le=4096, description="wächst die Auslagerung schneller, gilt der Rechner "
                                          "als knapp; 0 schaltet ab")
    swap_zuwachs_fenster_s: float = Field(default=60, ge=5, le=3600, description="über diesen Zeitraum wird der Zuwachs gemessen")


def grenze_gb(v: Vorlage, s: Speicherschutz) -> float:
    """Die Höchstgrenze eines Dienstes in GiB."""
    return v.speicher_grenze_gb if v.speicher_grenze_gb is not None else round(v.speicher_gb * s.faktor + s.zuschlag_gb, 2)


def _gb(wert: float, stellen: int = 1) -> str:
    return f"{wert:.{stellen}f}".replace(".", ",")


class Eingriff(BaseModel):
    """Ein Dienst, den die Wache beendet."""

    kennung: str
    name: str
    anlass: str                       # grenze oder knapp
    gb: float
    grenze_gb: float
    satz: str                         # Grund in Worten, für Stand, Protokoll, Oberfläche und Leitstelle


Verlauf = Sequence[tuple[float, Systemspeicher]]     # (Uhrzeit in s, Lage), älteste zuerst, die jüngste ist jetzt


def knapp(verlauf: Verlauf, s: Speicherschutz) -> str | None:
    """Warum der Rechner knapp ist; None, wenn er es nicht ist. Aus der jüngsten Lage: freier Anteil, kritischer Druck,
    Auslagerung über der Obergrenze; aus dem Verlauf: Auslagerung wächst schnell, oder sie liegt über dem Geduldeten
    länger als geduldet (wahlweise nur, solange das System dabei Druck meldet)."""
    if not verlauf:
        return None
    jetzt, lage = verlauf[-1]
    if lage.frei_prozent is not None and lage.frei_prozent < s.frei_mindestens_prozent:
        return f"der Rechner hat nur noch {lage.frei_prozent:.0f} Prozent frei"
    if s.druck_kritisch and lage.druck == Druckstufe.KRITISCH:
        return "das System meldet kritischen Speicherdruck"
    if lage.swap_gb is None:
        return None
    if s.swap_hoechstens_gb and lage.swap_gb > s.swap_hoechstens_gb:
        return f"{_gb(lage.swap_gb)} GB ausgelagert, mehr als {_gb(s.swap_hoechstens_gb)} GB"
    if s.swap_zuwachs_gb_je_min:
        frueh = [(t, l) for t, l in verlauf if t >= jetzt - s.swap_zuwachs_fenster_s and l.swap_gb is not None]
        if frueh and jetzt - frueh[0][0] >= s.swap_zuwachs_fenster_s / 2:
            je_min = (lage.swap_gb - frueh[0][1].swap_gb) / ((jetzt - frueh[0][0]) / 60)
            if je_min > s.swap_zuwachs_gb_je_min:
                return f"die Auslagerung wächst um {_gb(je_min)} GB je Minute (auf {_gb(lage.swap_gb)} GB)"
    fenster = [l for t, l in verlauf if t >= jetzt - s.swap_geduldet_s]
    abgedeckt = verlauf[0][0] <= jetzt - s.swap_geduldet_s
    if abgedeckt and fenster and all(l.swap_gb is not None and l.swap_gb > s.swap_geduldet_gb
                                      and (not s.swap_dauer_nur_bei_druck or l.druck != Druckstufe.NORMAL) for l in fenster):
        return (f"seit über {s.swap_geduldet_s / 60:.0f} Minuten mehr als {_gb(s.swap_geduldet_gb)} GB ausgelagert "
                f"(jetzt {_gb(lage.swap_gb)} GB)")
    return None


def entscheiden(gemessen: list[tuple[Dienst, float]], verlauf: Verlauf, s: Speicherschutz) -> list[Eingriff]:
    """Reine Entscheidung über Messwerte (GiB je Dienst) und Systemlage: alle über ihrer Grenze; ist keiner darüber und
    der Rechner knapp, der eine mit dem größten Überschuss über seinem angemeldeten Speicher."""
    aus = []
    for d, gb in gemessen:
        g = grenze_gb(d.vorlage, s)
        if gb > g:
            stellen = 1 if _gb(gb) != _gb(g) else 3          # knapp darüber: so genau, dass man den Unterschied sieht
            aus.append(Eingriff(kennung=d.kennung, name=d.vorlage.name, anlass="grenze", gb=round(gb, 3), grenze_gb=g,
                                satz=f"abgebrochen: Speicher, {_gb(gb, stellen)} GB belegt, Grenze {_gb(g, stellen)} GB "
                                     f"(angemeldet {_gb(d.vorlage.speicher_gb)} GB)"))
    if aus:
        return aus
    warum = knapp(verlauf, s)
    if warum is None:
        return []
    ueber = sorted(((gb - d.vorlage.speicher_gb, d, gb) for d, gb in gemessen), key=lambda t: t[0], reverse=True)
    if not ueber or ueber[0][0] < s.ueberschuss_mindestens_gb:
        return []
    _, d, gb = ueber[0]
    return [Eingriff(kennung=d.kennung, name=d.vorlage.name, anlass="knapp", gb=round(gb, 2), grenze_gb=grenze_gb(d.vorlage, s),
                     satz=f"abgebrochen: Speicher, {warum}; der Dienst lag mit {_gb(gb)} GB am weitesten über seinen "
                          f"angemeldeten {_gb(d.vorlage.speicher_gb)} GB")]


class Speicherwache:
    """Misst im eigenen Takt und greift über den Verwalter ein. `system` liefert die Lage des Rechners (in Tests
    nachgestellt)."""

    def __init__(self, verwalter: Verwalter, system: Callable[[], Systemspeicher] = systemspeicher,
                 uhr: Callable[[], float] = time.monotonic) -> None:
        self.verwalter = verwalter
        self.system = system
        self.uhr = uhr
        self.verlauf: deque[tuple[float, Systemspeicher]] = deque()

    @property
    def letzte_lage(self) -> Systemspeicher | None:
        return self.verlauf[-1][1] if self.verlauf else None

    def _merken(self, lage: Systemspeicher, s: Speicherschutz) -> None:
        """Die Lage in den Verlauf; behalten wird, was die längste Frist braucht."""
        jetzt = self.uhr()
        self.verlauf.append((jetzt, lage))
        aelteste = jetzt - max(s.swap_geduldet_s, s.swap_zuwachs_fenster_s) - 2 * s.takt_s
        while len(self.verlauf) > 2 and self.verlauf[1][0] <= aelteste:
            self.verlauf.popleft()

    def _messen(self, dienste: list[Dienst]) -> list[tuple[Dienst, float]]:
        tabelle = Prozesstabelle()
        aus = []
        for d in dienste:
            pids = d.prozesse(tabelle)
            if pids:
                aus.append((d, sum(prozess_bytes(p) for p in pids) / GIB))
        return aus

    async def runde(self) -> list[Eingriff]:
        """Einmal messen und, wo nötig, eingreifen; liefert die Eingriffe."""
        s = self.verwalter.speicherschutz
        if s is None or not s.aktiv:
            return []
        self._merken(await asyncio.to_thread(self.system), s)      # auch ohne laufende Dienste: Fristen brauchen den Verlauf
        laufend = [d for d in self.verwalter.dienste.values()
                   if d.zustand in (Zustand.LAEUFT, Zustand.STARTET, Zustand.STOPPT) and d.stand.sperre is None]
        if not laufend:
            return []
        gemessen = await asyncio.to_thread(self._messen, laufend)
        eingriffe = entscheiden(gemessen, self.verlauf, s)
        for e in eingriffe:
            await self.verwalter.speicher_abbrechen(e)
        return eingriffe

    async def schleife(self) -> None:
        while True:
            s = self.verwalter.speicherschutz
            try:
                await self.runde()
            except Exception:                                   # die Wache darf nie sterben
                log.exception("Fehler in der Speicherwache")
            await asyncio.sleep(s.takt_s if s is not None else 5.0)


VorEingriff = Callable[[Eingriff], Awaitable[None]]
