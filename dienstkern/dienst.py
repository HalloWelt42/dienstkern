"""Ein verwalteter Dienst: starten, anhalten, beobachten.

Isolation je Dienst: eigene Sitzung und Prozessgruppe (Anhalten trifft genau diese Gruppe), eigener Arbeitsordner,
eigene Umgebung (nur Pfad, Heimordner, Sprache und was die Vorlage nennt), eigene Protokolldatei.
Ein Dienst, der schon auf seinem Port antwortet, ohne von hier gestartet zu sein, gilt als fremd gestartet: er wird
übernommen, nicht doppelt gestartet; Anhalten trifft dann den lauschenden Prozess und seine Nachfahren, nie dessen
ganze Gruppe. Befehlsdienste (steuerung = befehle) werden per Befehl geprüft und angehalten, nie per Signal.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from pydantic import BaseModel

from .messen import GIB, Prozesstabelle, lauschender_prozess, prozess_bytes
from .vorlage import Steuerung, Vorlage, erweitern, umgebung_lesen
from .zustand import DienstStand, Soll, Zustand

PROTOKOLL_GRENZE = 20 * 1024 * 1024
WARTESCHRITT_S = 0.5


class Bericht(BaseModel):
    """Stand eines Dienstes, wie ihn Oberflächen und Anbindungen sehen."""

    kennung: str
    name: str
    port: int | None = None
    zustand: Zustand
    fremd: bool = False                 # läuft, aber nicht von hier gestartet
    gb_jetzt: float | None = None       # gemessen, solange er läuft; sonst None
    bedarf_gb: float                    # was er laufend braucht: höchster gemessener Wert, mindestens die Schätzung
    spitze_gb: float | None = None
    cpu_prozent: float | None = None    # Prozessorlast, Prozent eines Kerns, solange er läuft
    aktiv: bool = False                 # arbeitet gerade (Prozessorlast über der Schwelle des Dienstes)
    soll: Soll | None = None
    meldung: str = ""


def _lebt(pgid: int, tabelle: Prozesstabelle) -> bool:
    return bool(tabelle.in_gruppe(pgid))


def _signal_gruppe(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):   # macOS: Gruppe nur noch aus Zombies meldet "nicht erlaubt"
        pass


def _signal_prozesse(pids: list[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


class Dienst:
    """Ein verwalteter Dienst; `herkunft` steht im Protokoll bei jedem Start (wer ihn gestartet hat)."""

    def __init__(self, vorlage: Vorlage, protokolle: Path, stand: DienstStand, herkunft: str = "Dienstkern") -> None:
        self.vorlage = vorlage
        self.protokolle = protokolle
        self.stand = stand
        self.herkunft = herkunft
        self.zustand = Zustand.AUS
        self.fremd_pid: int | None = None
        self.prozess: subprocess.Popen | None = None    # nur zum Abholen des Rückgabewerts
        self.meldung = ""
        self.gb: float | None = None
        self.sperre = asyncio.Lock()
        self.abstuerze: deque[float] = deque()
        self.neustart_um: float | None = None
        self.cpu_prozent: float | None = None             # Prozessorlast seit der letzten Prüfung, Prozent eines Kerns
        self._rechenzeit: tuple[float, float] | None = None  # (Uhrzeit, Summe Rechenzeit) der letzten Prüfung

    @property
    def kennung(self) -> str:
        return self.vorlage.kennung

    # ── Beobachten ───────────────────────────────────────────────────────
    def erreichbar(self) -> bool:
        v = self.vorlage
        if v.pruefbefehl:
            try:
                return subprocess.run([erweitern(t, self._umgebung()) for t in v.pruefbefehl], cwd=v.ordner, env=self._umgebung(),
                                      stdin=subprocess.DEVNULL, capture_output=True, timeout=15).returncode == 0
            except (OSError, subprocess.SubprocessError):
                return False
        if v.pruefadresse:
            try:
                with urllib.request.urlopen(v.pruefadresse, timeout=2) as antwort:
                    return antwort.status < 500
            except urllib.error.HTTPError as f:
                return f.code < 500
            except (OSError, ValueError):
                return False
        if v.port:
            for familie, adresse in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
                try:
                    with socket.socket(familie, socket.SOCK_STREAM) as s:
                        s.settimeout(0.5)
                        if s.connect_ex((adresse, v.port)) == 0:
                            return True
                except OSError:
                    continue
            return False
        return self.stand.gruppe is not None

    def pruefen(self, tabelle: Prozesstabelle) -> str | None:
        """Stand aus der Prozesstabelle und der Erreichbarkeit. Rückgabe: Absturzmeldung, wenn eine eigene Gruppe
        unerwartet endete."""
        if self.vorlage.steuerung == Steuerung.BEFEHLE:
            return self._pruefen_befehle(tabelle)
        if self.prozess and self.prozess.poll() is not None:
            code, self.prozess = self.prozess.returncode, None
        else:
            code = None
        g = self.stand.gruppe
        if g is not None and _lebt(g, tabelle):
            self.fremd_pid = None
            if self.zustand == Zustand.AUS or self.zustand == Zustand.GESTOERT:
                self.zustand = Zustand.LAEUFT
            self._messen(tabelle.in_gruppe(g), tabelle)
            return None
        if g is not None:                                   # eigene Gruppe ist weg
            self.stand.gruppe = None
            self._messen([], tabelle)
            if self.zustand in (Zustand.LAEUFT, Zustand.STARTET):
                self.zustand = Zustand.GESTOERT
                self.meldung = f"unerwartet beendet{f' (Code {code})' if code is not None else ''}{self._letzte_zeile()}"
                return self.meldung
            self.zustand = Zustand.AUS
            return None
        if self.zustand == Zustand.GESTOERT:
            return None
        if self.vorlage.port and self.erreichbar():
            self.fremd_pid = lauschender_prozess(self.vorlage.port)
            self.zustand = Zustand.LAEUFT
            self._messen(tabelle.mit_nachfahren(self.fremd_pid) if self.fremd_pid else [], tabelle)
            self.meldung = "von außen gestartet" if self.fremd_pid else "von außen gestartet, Prozess nicht lesbar"
        else:
            self.fremd_pid = None
            self._messen([], tabelle)
            if self.zustand == Zustand.LAEUFT:
                self.meldung = "von außen beendet"
            self.zustand = Zustand.AUS
        return None

    def _pruefen_befehle(self, tabelle: Prozesstabelle) -> None:
        """Befehlsdienst: er läuft, wenn die Prüfung gelingt; gemessen wird, was zum Prozessmuster passt."""
        if self.erreichbar():
            if self.zustand != Zustand.LAEUFT:
                self.zustand, self.meldung = Zustand.LAEUFT, ""
            self._messen(tabelle.passend(self.vorlage.prozessmuster) if self.vorlage.prozessmuster else [], tabelle)
        else:
            if self.zustand == Zustand.LAEUFT:
                self.meldung = "läuft nicht mehr"
            if self.zustand != Zustand.GESTOERT:
                self.zustand = Zustand.AUS
            self._messen([], tabelle)
        return None

    def _befehl_ausfuehren(self, befehl: list[str], grenze_s: float) -> None:
        """Einmaliger Befehl (Start oder Anhalten eines Befehlsdienstes), Ausgabe ins Protokoll des Dienstes, in eigener
        Sitzung, damit nichts davon an der Prozessgruppe des Programms hängt."""
        env = self._umgebung()
        with self._protokoll_oeffnen() as aus:
            aus.write(f"\n--- {' '.join(befehl)} ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---\n".encode())
            aus.flush()
            subprocess.run([erweitern(t, env) for t in befehl], cwd=self.vorlage.ordner, env=env, stdin=subprocess.DEVNULL, stdout=aus,
                           stderr=subprocess.STDOUT, timeout=grenze_s, check=True, start_new_session=True)

    def _messen(self, pids: list[int], tabelle: Prozesstabelle) -> None:
        """Speicher und Prozessorlast der Prozesse des Dienstes; ohne Prozesse ist beides leer."""
        if not pids:
            self.gb, self.cpu_prozent, self._rechenzeit = None, None, None
            return
        self.gb = sum(prozess_bytes(p) for p in pids) / GIB
        jetzt, summe = time.monotonic(), sum(tabelle.rechenzeit_s.get(p, 0.0) for p in pids)
        if self._rechenzeit and jetzt > self._rechenzeit[0]:
            self.cpu_prozent = round(max(0.0, summe - self._rechenzeit[1]) / (jetzt - self._rechenzeit[0]) * 100, 1)
        self._rechenzeit = (jetzt, summe)

    @property
    def aktiv(self) -> bool:
        return self.cpu_prozent is not None and self.cpu_prozent >= self.vorlage.aktiv_ab_prozent

    @property
    def bedarf_gb(self) -> float:
        """Was der Dienst laufend braucht: der höchste je gemessene Wert, mindestens die Schätzung der Dienstdatei.
        Dienste laden ihre Modelle oft erst bei Bedarf; der Wert gleich nach dem Start täuscht."""
        return round(max(self.stand.spitze_gb or 0.0, self.vorlage.speicher_gb, self.gb or 0.0), 2)

    def _protokolldatei(self) -> Path:
        return self.protokolle / f"{self.kennung}.log"

    def _letzte_zeile(self) -> str:
        try:
            zeilen = [z for z in self._protokolldatei().read_text(encoding="utf-8", errors="replace").splitlines() if z.strip()]
            return f"; zuletzt: {zeilen[-1].strip()[:160]}" if zeilen else ""
        except OSError:
            return ""

    # ── Starten ──────────────────────────────────────────────────────────
    def _umgebung(self) -> dict[str, str]:
        v = self.vorlage
        env = {"PATH": v.pfad, "HOME": os.environ.get("HOME", ""), "USER": os.environ.get("USER", ""),
               "LANG": os.environ.get("LANG", "de_DE.UTF-8"), "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        if v.umgebung_datei:
            env.update(umgebung_lesen(v.umgebung_datei))
        for name, wert in v.umgebung.items():
            env[name] = erweitern(wert, env)
        return env

    def _protokoll_oeffnen(self):
        datei = self._protokolldatei()
        datei.parent.mkdir(parents=True, exist_ok=True)
        if datei.exists() and datei.stat().st_size > PROTOKOLL_GRENZE:
            datei.replace(datei.with_suffix(".log.1"))
        return open(datei, "ab")

    def _vorher(self, env: dict[str, str]) -> None:
        v = self.vorlage
        if not v.vorher:
            return
        befehl = [erweitern(t, env) for t in v.vorher]
        if "/" in befehl[0] and not os.path.isabs(befehl[0]):
            befehl[0] = str(v.ordner / befehl[0])
        with self._protokoll_oeffnen() as aus:
            subprocess.run(befehl, cwd=v.ordner, env=env, stdin=subprocess.DEVNULL, stdout=aus, stderr=subprocess.STDOUT,
                           timeout=v.startzeit_s, check=True)

    def _ausfuehren(self) -> None:
        v = self.vorlage
        env = self._umgebung()
        self._vorher(env)
        with self._protokoll_oeffnen() as aus:
            aus.write(f"\n--- gestartet von {self.herkunft} {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
            aus.flush()
            self.prozess = subprocess.Popen(v.programm(env), cwd=v.ordner, env=env, stdin=subprocess.DEVNULL, stdout=aus, stderr=subprocess.STDOUT,
                                            start_new_session=True)
        self.stand.gruppe = self.prozess.pid

    async def starten(self) -> tuple[bool, str]:
        async with self.sperre:
            self.neustart_um = None
            if self.zustand == Zustand.LAEUFT:
                return True, "läuft bereits"
            if self.vorlage.port and await asyncio.to_thread(self.erreichbar):
                return True, "läuft bereits (von außen gestartet)"
            if not self.vorlage.ordner.is_dir():
                return self._gestoert(f"Arbeitsordner fehlt: {self.vorlage.ordner}")
            self.zustand, self.meldung = Zustand.STARTET, ""
            if self.vorlage.steuerung == Steuerung.BEFEHLE:
                return await self._starten_befehle()
            try:
                await asyncio.to_thread(self._ausfuehren)
            except subprocess.CalledProcessError as f:
                return self._gestoert(f"Vorlauf scheiterte mit Code {f.returncode}{self._letzte_zeile()}")
            except (OSError, subprocess.SubprocessError) as f:
                return self._gestoert(f"Start scheiterte: {f}")
            return await self._warten_bis_erreichbar()

    async def _starten_befehle(self) -> tuple[bool, str]:
        v = self.vorlage
        beginn = time.monotonic()
        try:
            await asyncio.to_thread(self._vorher, self._umgebung())
            await asyncio.to_thread(self._befehl_ausfuehren, v.befehl, v.startzeit_s)
        except subprocess.CalledProcessError as f:
            return self._gestoert(f"Startbefehl endete mit Code {f.returncode}{self._letzte_zeile()}")
        except (OSError, subprocess.SubprocessError) as f:
            return self._gestoert(f"Startbefehl scheiterte: {f}")
        while time.monotonic() - beginn < v.startzeit_s:
            if await asyncio.to_thread(self.erreichbar):
                self.zustand, self.meldung = Zustand.LAEUFT, ""
                return True, f"gestartet in {time.monotonic() - beginn:.1f} s"
            await asyncio.sleep(1.0)
        return self._gestoert(f"nach {v.startzeit_s:.0f} s nicht erreichbar{self._letzte_zeile()}")

    async def _warten_bis_erreichbar(self) -> tuple[bool, str]:
        beginn = time.monotonic()
        while time.monotonic() - beginn < self.vorlage.startzeit_s:
            if self.prozess and self.prozess.poll() is not None:
                code = self.prozess.returncode
                self.prozess, self.stand.gruppe = None, None
                return self._gestoert(f"beim Start beendet mit Code {code}{self._letzte_zeile()}")
            if (self.vorlage.port or self.vorlage.pruefadresse) and await asyncio.to_thread(self.erreichbar):
                break
            if not (self.vorlage.port or self.vorlage.pruefadresse) and time.monotonic() - beginn >= 1:
                break
            await asyncio.sleep(WARTESCHRITT_S)
        else:
            await self._gruppe_beenden()
            return self._gestoert(f"nach {self.vorlage.startzeit_s:.0f} s nicht erreichbar; wieder beendet{self._letzte_zeile()}")
        self.zustand, self.meldung = Zustand.LAEUFT, ""
        return True, f"gestartet in {time.monotonic() - beginn:.1f} s"

    def _gestoert(self, meldung: str) -> tuple[bool, str]:
        self.zustand, self.meldung = Zustand.GESTOERT, meldung
        return False, meldung

    # ── Anhalten ─────────────────────────────────────────────────────────
    async def anhalten(self) -> tuple[bool, str]:
        async with self.sperre:
            self.neustart_um = None
            if self.vorlage.anhaltebefehl and (self.vorlage.steuerung == Steuerung.BEFEHLE or self.fremd_pid):
                return await self._anhalten_befehl()
            if self.stand.gruppe is not None:
                self.zustand = Zustand.STOPPT
                hart = await self._gruppe_beenden()
            elif self.fremd_pid:
                self.zustand = Zustand.STOPPT
                hart = await self._fremd_beenden(self.fremd_pid)
            else:
                self.zustand = Zustand.AUS
                return True, "war schon aus"
            self.zustand, self.gb, self.fremd_pid, self.meldung = Zustand.AUS, None, None, ""
            return True, "hart beendet" if hart else "beendet"

    async def _anhalten_befehl(self) -> tuple[bool, str]:
        """Anhalten mit dem eigenen Befehl, nie per Signal: bei Containern träfe ein Signal sonst die Portweiterleitung."""
        if not await asyncio.to_thread(self.erreichbar):
            self.zustand = Zustand.AUS
            return True, "war schon aus"
        self.zustand = Zustand.STOPPT
        try:
            await asyncio.to_thread(self._befehl_ausfuehren, self.vorlage.anhaltebefehl, self.vorlage.anhaltezeit_s)
        except (OSError, subprocess.SubprocessError) as f:
            return self._gestoert(f"Anhaltebefehl scheiterte: {f}{self._letzte_zeile()}")
        beginn = time.monotonic()
        while time.monotonic() - beginn < self.vorlage.anhaltezeit_s:
            if not await asyncio.to_thread(self.erreichbar):
                self.zustand, self.gb, self.fremd_pid, self.meldung = Zustand.AUS, None, None, ""
                return True, "beendet"
            await asyncio.sleep(1.0)
        return self._gestoert(f"läuft nach dem Anhaltebefehl noch ({self.vorlage.anhaltezeit_s:.0f} s gewartet)")

    async def _gruppe_beenden(self) -> bool:
        g = self.stand.gruppe
        if g is None:
            return False
        _signal_gruppe(g, signal.SIGTERM)
        hart = not await self._warten(lambda t: not _lebt(g, t))
        if hart:
            _signal_gruppe(g, signal.SIGKILL)
            await self._warten(lambda t: not _lebt(g, t))
        if self.prozess:
            await asyncio.to_thread(self.prozess.wait)
            self.prozess = None
        self.stand.gruppe = None
        return hart

    async def _fremd_beenden(self, pid: int) -> bool:
        pids = (await asyncio.to_thread(Prozesstabelle)).mit_nachfahren(pid)
        _signal_prozesse(pids, signal.SIGTERM)
        hart = not await self._warten(lambda t: not any(p in t.eltern for p in pids))
        if hart:
            _signal_prozesse(pids, signal.SIGKILL)
            await self._warten(lambda t: not any(p in t.eltern for p in pids))
        return hart

    async def _warten(self, fertig) -> bool:
        beginn = time.monotonic()
        while time.monotonic() - beginn < self.vorlage.anhaltezeit_s:
            if self.prozess:
                self.prozess.poll()                         # eigenes Kind abholen, sonst bleibt es als Zombie in der Tabelle
            if fertig(await asyncio.to_thread(Prozesstabelle)):
                return True
            await asyncio.sleep(WARTESCHRITT_S)
        return False

    # ── Bericht ──────────────────────────────────────────────────────────
    def bericht(self) -> Bericht:
        laeuft = self.zustand in (Zustand.LAEUFT, Zustand.STARTET, Zustand.STOPPT)
        gemessen = laeuft and self.gb is not None
        return Bericht(kennung=self.kennung, name=self.vorlage.name, port=self.vorlage.port, zustand=self.zustand, fremd=self.fremd_pid is not None,
                       gb_jetzt=round(self.gb, 2) if gemessen else None, bedarf_gb=self.bedarf_gb, spitze_gb=self.stand.spitze_gb,
                       cpu_prozent=self.cpu_prozent if gemessen else None, aktiv=laeuft and self.aktiv, soll=self.stand.soll, meldung=self.meldung)

    def protokolldatei(self) -> Path:
        return self._protokolldatei()
