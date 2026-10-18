"""Alle Dienste eines Programms: Vorlagen lesen, prüfen, nach Absturz begrenzt neu starten, Sollzustand halten.

Woher die Vorlagen kommen, entscheidet das Programm (eine `quelle`, die bei jeder Runde gefragt wird); der Verwalter
kennt weder Dateiformat noch Anbindungen. Läuft unabhängig von Oberfläche und Netz.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .dienst import Bericht, Dienst
from .messen import Prozesstabelle
from .vorlage import Ladefehler, Vorlage
from .zustand import DienstStand, Soll, Zustand, stand_lesen, stand_speichern

log = logging.getLogger("dienstkern")

Quelle = Callable[[], tuple[list[Vorlage], list[Ladefehler]]]


class Neustartregeln(BaseModel):
    pausen_s: list[float] = Field(default=[5, 30, 120], min_length=1, description="Pausen vor dem 1., 2., 3. Neustart nach einem Absturz")
    fenster_s: float = Field(default=600, gt=0, description="Zeitraum, in dem Abstürze gezählt werden")
    im_fenster: int = Field(default=3, ge=0, description="mehr Abstürze im Zeitraum: der Dienst bleibt aus")


class Ergebnis(BaseModel):
    ziel: str
    erfolg: bool
    meldung: str = ""


class Verwalter:
    """`herkunft` ergänzt im Protokoll jedes Dienstes die Zeile beim Start ("gestartet vom Maschinenraum")."""

    def __init__(self, quelle: Quelle, protokolle: Path, zustand_datei: Path, regeln: Neustartregeln | None = None,
                 takt_s: float = 5.0, herkunft: str = "vom Dienstkern") -> None:
        self.quelle = quelle
        self.protokolle = protokolle
        self.zustand_datei = zustand_datei
        self._regeln = regeln or Neustartregeln()
        self._takt_s = takt_s
        self.herkunft = herkunft
        self.dienste: dict[str, Dienst] = {}
        self.ladefehler: list[Ladefehler] = []
        self.stand = stand_lesen(zustand_datei)
        self.aenderung = asyncio.Event()
        self._aufgaben: set[asyncio.Task] = set()

    @property
    def regeln(self) -> Neustartregeln:
        return self._regeln

    @property
    def takt_s(self) -> float:
        return self._takt_s

    def _stand_speichern(self) -> None:
        stand_speichern(self.zustand_datei, self.stand)

    def _vorlagen_lesen(self) -> bool:
        vorlagen, fehler = self.quelle()
        geaendert = [f.model_dump() for f in fehler] != [f.model_dump() for f in self.ladefehler]
        for f in fehler:
            if f not in self.ladefehler:
                log.warning("%s fehlerhaft: %s", f.datei, f.meldung)
        self.ladefehler = fehler
        neu = {v.kennung: v for v in vorlagen}
        for k in list(self.dienste):
            if k not in neu:
                d = self.dienste.pop(k)
                if d.stand.gruppe is not None:
                    self._nebenbei(d.anhalten())
                geaendert = True
        for k, v in neu.items():
            if k in self.dienste:
                if self.dienste[k].vorlage != v:
                    self.dienste[k].vorlage, geaendert = v, True
            else:
                stand = self.stand.dienste.setdefault(k, DienstStand())
                self.dienste[k] = Dienst(v, self.protokolle, stand, self.herkunft)
                geaendert = True
        return geaendert

    def _nebenbei(self, coro: Coroutine[Any, Any, Any]) -> None:
        t = asyncio.create_task(coro)
        self._aufgaben.add(t)
        t.add_done_callback(self._aufgaben.discard)

    async def runde(self) -> None:
        """Einmal alles prüfen: Vorlagen, Prozesse, Abstürze, fällige Neustarts, Höchstwerte."""
        geaendert = self._vorlagen_lesen()
        tabelle = await asyncio.to_thread(Prozesstabelle)
        jetzt = time.monotonic()
        for d in self.dienste.values():
            if d.sperre.locked():
                continue
            vorher = d.bericht()
            absturz = await asyncio.to_thread(d.pruefen, tabelle)
            if absturz:
                self._absturz(d, jetzt)
            if d.neustart_um is not None and jetzt >= d.neustart_um:
                d.neustart_um = None
                self._nebenbei(self._neustarten(d))
            d.arbeit_messen()
            # Messwerte schwanken ständig; gemeldet wird nur, was sich in der Sache ändert (Zustand, Arbeit, Bedarf)
            fluechtig = {"gb_jetzt", "cpu_prozent", "spitze_gb"}
            geaendert = geaendert or d.bericht().model_dump(exclude=fluechtig) != vorher.model_dump(exclude=fluechtig)
        self._stand_speichern()
        if geaendert:
            self.aenderung.set()

    def _absturz(self, d: Dienst, jetzt: float) -> None:
        k = self.regeln
        log.warning("%s: %s", d.vorlage.name, d.meldung)
        if d.stand.soll != Soll.LAEUFT or not d.vorlage.neustart:
            return
        while d.abstuerze and jetzt - d.abstuerze[0] > k.fenster_s:
            d.abstuerze.popleft()
        d.abstuerze.append(jetzt)
        if len(d.abstuerze) > k.im_fenster:
            d.stand.soll = Soll.AUS
            d.meldung += f"; {len(d.abstuerze)} Abstürze in {k.fenster_s:.0f} s, bleibt aus bis zum nächsten Start von Hand"
            return
        pause = k.pausen_s[min(len(d.abstuerze) - 1, len(k.pausen_s) - 1)]
        d.neustart_um = jetzt + pause
        d.meldung += f"; Neustart in {pause:.0f} s"

    async def _mit_abhaengigkeiten(self, d: Dienst, kette: tuple[str, ...] = ()) -> tuple[bool, str]:
        """Erst starten, was der Dienst braucht (der Reihe nach, ohne Kreise), dann ihn selbst."""
        for k in d.vorlage.braucht:
            vor = self.dienste.get(k)
            if vor is None:
                return d._gestoert(f"braucht {k}, den es hier nicht gibt")
            if k in kette or k == d.kennung:
                return d._gestoert(f"Abhängigkeiten im Kreis: {' -> '.join((*kette, d.kennung, k))}")
            if vor.zustand != Zustand.LAEUFT:
                vor.stand.soll = Soll.LAEUFT
                ok, meldung = await self._mit_abhaengigkeiten(vor, (*kette, d.kennung))
                if not ok:
                    return d._gestoert(f"{vor.vorlage.name} startet nicht: {meldung}")
        return await d.starten()

    async def _neustarten(self, d: Dienst) -> None:
        ok, meldung = await self._mit_abhaengigkeiten(d)
        log.info("%s: Neustart %s (%s)", d.vorlage.name, "gelungen" if ok else "gescheitert", meldung)
        self.aenderung.set()

    async def hochfahren(self) -> None:
        """Nach dem Start des Programms: Vorlagen lesen, Lage prüfen, starten was laufen soll."""
        await self.runde()
        for d in self.dienste.values():
            if d.stand.soll == Soll.LAEUFT and d.zustand == Zustand.AUS:
                log.info("%s soll laufen: starte", d.vorlage.name)
                self._nebenbei(self._neustarten(d))

    async def schleife(self) -> None:
        await self.hochfahren()
        while True:
            await asyncio.sleep(self.takt_s)
            try:
                await self.runde()
            except Exception:                                   # die Schleife darf nie sterben
                log.exception("Fehler in der Dienstprüfung")

    def _abschliessen(self, d: Dienst, was: str, ok: bool, meldung: str) -> Ergebnis:
        self._stand_speichern()
        self.aenderung.set()
        log.info("%s %s: %s", was, d.vorlage.name, meldung)
        return Ergebnis(ziel=d.kennung, erfolg=ok, meldung=meldung)

    async def starten(self, kennung: str, *, von_hand: bool = True) -> Ergebnis:
        """Starten, mit allem, was er braucht; er soll von nun an laufen (auch nach Neustarts). Nur ein Start von Hand
        löscht die Abstürze; ein Dienst, der nach zu vielen Abstürzen aus bleibt, startet auf Befehl von außen nicht."""
        d = self.dienste.get(kennung)
        if d is None:
            return Ergebnis(ziel=kennung, erfolg=False, meldung=f"unbekannter Dienst {kennung}")
        if not von_hand and len(d.abstuerze) > self.regeln.im_fenster:
            return Ergebnis(ziel=kennung, erfolg=False,
                            meldung=f"{d.vorlage.name} bleibt nach {len(d.abstuerze)} Abstürzen aus, bis er von Hand startet")
        d.stand.soll = Soll.LAEUFT
        if von_hand:
            d.abstuerze.clear()
        ok, meldung = await self._mit_abhaengigkeiten(d)
        return self._abschliessen(d, "starten", ok, meldung)

    async def anhalten(self, kennung: str) -> Ergebnis:
        d = self.dienste.get(kennung)
        if d is None:
            return Ergebnis(ziel=kennung, erfolg=False, meldung=f"unbekannter Dienst {kennung}")
        d.stand.soll = Soll.AUS
        ok, meldung = await d.anhalten()
        return self._abschliessen(d, "anhalten", ok, meldung)

    async def neu_starten(self, kennung: str) -> Ergebnis:
        aus = await self.anhalten(kennung)
        if not aus.erfolg:
            return aus
        return await self.starten(kennung)

    def reihenfolge(self, kennungen: list[str]) -> list[str]:
        """Startreihenfolge: wer gebraucht wird, zuerst (innerhalb der genannten Kennungen; Kreise brechen ab, ohne
        etwas zu verlieren). Anhalten geht in umgekehrter Folge."""
        gewaehlt, fertig, folge = set(kennungen), set(), []

        def besuchen(k: str, pfad: frozenset[str]) -> None:
            if k in fertig or k in pfad:
                return
            d = self.dienste.get(k)
            for vor in (d.vorlage.braucht if d else []):
                if vor in gewaehlt:
                    besuchen(vor, pfad | {k})
            fertig.add(k)
            folge.append(k)

        for k in kennungen:
            besuchen(k, frozenset())
        return folge

    def berichte(self) -> list[Bericht]:
        return [d.bericht() for d in self.dienste.values()]

    async def beenden(self) -> None:
        for t in list(self._aufgaben):
            t.cancel()
