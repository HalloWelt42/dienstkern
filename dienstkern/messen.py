"""Prozessmessung mit Bordmitteln des Systems: Prozesstabelle (Gruppen, Nachfahren, Rechenzeit), Speicher eines
Prozesses, lesbarer Programmname und der Prozess hinter einem lauschenden Port.

Prozessspeicher: auf dem Mac der physische Fußabdruck (wie die Aktivitätsanzeige; RSS zählt geteilte Seiten falsch),
auf Linux PSS. Einheit überall GiB, wie die Systemanzeigen.

Daneben auf dem Mac, aus demselben Aufruf: eingeblendete Dateien im Arbeitsspeicher (Seiten, die resident sind, aber
nicht zum Fußabdruck zählen, vor allem in den Speicher eingeblendete Dateien wie die Daten einer Datenbank). Das System
gibt sie bei Bedarf frei; sie zählen deshalb nicht zum festen Speicher.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import NamedTuple

GIB = 1024**3
MAC = platform.system() == "Darwin"


def befehl(*args: str) -> str:
    """Systembefehl mit neutraler Spracheinstellung: Zahlen kommen dann mit Punkt, egal wie der Rechner eingestellt ist."""
    return subprocess.run(args, capture_output=True, text=True, timeout=10, check=True, env={**os.environ, "LC_ALL": "C"}).stdout


# ── Prozesse ─────────────────────────────────────────────────────────────
class _RusageV0(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16), ("user_time", ctypes.c_uint64), ("system_time", ctypes.c_uint64),
                ("pkg_idle_wkups", ctypes.c_uint64), ("interrupt_wkups", ctypes.c_uint64), ("pageins", ctypes.c_uint64),
                ("wired_size", ctypes.c_uint64), ("resident_size", ctypes.c_uint64), ("phys_footprint", ctypes.c_uint64),
                ("proc_start_abstime", ctypes.c_uint64), ("proc_exit_abstime", ctypes.c_uint64)]


_libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib") if MAC else None


class Prozessspeicher(NamedTuple):
    fest: int                       # Byte, fest belegt: Fußabdruck (Mac) oder PSS (Linux)
    eingeblendet: int | None        # Byte eingeblendeter Dateien im Arbeitsspeicher, die das System freigeben kann; None: hier nicht getrennt messbar


def prozess_speicher(pid: int) -> Prozessspeicher:
    """Speicher eines Prozesses mit einem Aufruf; (0, None), wenn er nicht (mehr) lesbar ist. Auf dem Mac ist der
    eingeblendete Anteil, was resident ist, aber nicht zum Fußabdruck zählt (nie unter 0: ausgelagerter oder
    komprimierter Speicher zählt zum Fußabdruck, nicht zum residenten). Auf Linux steckt er schon im PSS und wird nicht
    getrennt ausgewiesen."""
    if MAC:
        info = _RusageV0()
        if _libproc.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
            return Prozessspeicher(0, None)
        return Prozessspeicher(int(info.phys_footprint), max(0, int(info.resident_size) - int(info.phys_footprint)))
    return Prozessspeicher(_pss_linux(pid), None)


def prozess_bytes(pid: int) -> int:
    """Fest belegter Speicher eines Prozesses in Byte; 0, wenn er nicht (mehr) lesbar ist."""
    return prozess_speicher(pid).fest


# ── Rechner ──────────────────────────────────────────────────────────────
class Druckstufe(int, Enum):
    """Speicherdruck, wie das System ihn meldet (macOS kern.memorystatus_vm_pressure_level)."""

    NORMAL = 1
    WARNUNG = 2
    KRITISCH = 4


class Systemspeicher(NamedTuple):
    frei_prozent: float | None      # Anteil, den das System frei nennt; None: nicht lesbar
    druck: Druckstufe
    swap_gb: float | None = None    # belegte Auslagerung in GiB; None: nicht lesbar


class _XswUsage(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64), ("used", ctypes.c_uint64),
                ("pagesize", ctypes.c_uint32), ("encrypted", ctypes.c_bool)]


def _swap_mac() -> float | None:
    """vm.swapusage ohne neuen Prozess."""
    wert, groesse = _XswUsage(), ctypes.c_size_t(ctypes.sizeof(_XswUsage))
    if _libc.sysctlbyname(b"vm.swapusage", ctypes.byref(wert), ctypes.byref(groesse), None, 0) != 0:
        return None
    return wert.used / GIB


_libc = ctypes.CDLL(None) if MAC else None


def _sysctl_zahl(name: str) -> int | None:
    """Ganzzahl aus sysctl ohne neuen Prozess (die Speicherwache fragt jede Sekunde)."""
    wert, groesse = ctypes.c_int64(0), ctypes.c_size_t(8)
    if _libc.sysctlbyname(name.encode(), ctypes.byref(wert), ctypes.byref(groesse), None, 0) != 0:
        return None
    return int(wert.value) if groesse.value == 8 else int(wert.value & 0xFFFFFFFF)


def systemspeicher() -> Systemspeicher:
    """Mac: der freie Anteil, nach dem das System selbst Speicher zurückfordert (kern.memorystatus_level, wie
    `memory_pressure` ihn als "System-wide memory free percentage" zeigt), und die Druckstufe. Linux: MemAvailable je
    MemTotal; eine Druckstufe meldet Linux so nicht, sie bleibt normal. Dazu die belegte Auslagerung."""
    if MAC:
        frei, druck = _sysctl_zahl("kern.memorystatus_level"), _sysctl_zahl("kern.memorystatus_vm_pressure_level")
        stufe = Druckstufe(druck) if druck in (1, 2, 4) else Druckstufe.NORMAL
        return Systemspeicher(float(frei) if frei is not None else None, stufe, _swap_mac())
    try:
        werte = {z.split(":")[0]: int(z.split()[1]) for z in Path("/proc/meminfo").read_text().splitlines() if ":" in z}
        swap = (werte["SwapTotal"] - werte["SwapFree"]) * 1024 / GIB
        return Systemspeicher(round(100 * werte["MemAvailable"] / werte["MemTotal"], 1), Druckstufe.NORMAL, swap)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return Systemspeicher(None, Druckstufe.NORMAL)


def _pss_linux(pid: int) -> int:
    try:
        for zeile in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if zeile.startswith("Pss:"):
                return int(zeile.split()[1]) * 1024
    except OSError:
        pass
    try:
        for zeile in Path(f"/proc/{pid}/status").read_text().splitlines():
            if zeile.startswith("VmRSS:"):
                return int(zeile.split()[1]) * 1024
    except OSError:
        pass
    return 0


def rechenzeit(text: str) -> float:
    """Prozessorzeit aus `ps -o time`: [[TT-]SS:]MM:SS[.hh] in Sekunden."""
    tage = 0
    if "-" in text:
        t, text = text.split("-", 1)
        tage = int(t)
    teile = [float(x) for x in text.split(":")]
    sekunden = 0.0
    for teil in teile:
        sekunden = sekunden * 60 + teil
    return tage * 86400 + sekunden


class Prozesstabelle:
    """Eine Momentaufnahme aller lebenden Prozesse (pid, Eltern, Gruppe), um Gruppen und Nachfahren zu finden.
    Zombies (beendet, noch nicht abgeholt) zählen nicht als lebend."""

    def __init__(self) -> None:
        self.eltern: dict[int, int] = {}
        self.gruppe: dict[int, int] = {}
        self.rechenzeit_s: dict[int, float] = {}      # verbrauchte Prozessorzeit seit Prozessstart
        self.befehlszeile: dict[int, str] = {}
        for pid, ppid, pgid, zustand, sekunden, zeile in (self._ps() if MAC else self._proc()):
            if not zustand.startswith("Z"):
                self.eltern[pid], self.gruppe[pid], self.rechenzeit_s[pid], self.befehlszeile[pid] = ppid, pgid, sekunden, zeile

    def passend(self, muster: str) -> list[int]:
        """Prozesse, deren Befehlszeile zum Muster passt."""
        m = re.compile(muster)
        return [p for p, z in self.befehlszeile.items() if m.search(z)]

    @staticmethod
    def _ps():
        for zeile in befehl("ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,time=,command=").splitlines():
            teile = zeile.split(None, 5)
            if len(teile) >= 5:
                yield int(teile[0]), int(teile[1]), int(teile[2]), teile[3], rechenzeit(teile[4]), teile[5] if len(teile) > 5 else ""

    @staticmethod
    def _proc():
        """Linux ohne Zusatzprogramme: /proc/<pid>/stat, Felder nach dem Programmnamen in Klammern."""
        takt = os.sysconf("SC_CLK_TCK")
        for eintrag in Path("/proc").iterdir():
            if not eintrag.name.isdigit():
                continue
            try:
                rest = (eintrag / "stat").read_text().rsplit(")", 1)[1].split()
                zeile = (eintrag / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
                yield int(eintrag.name), int(rest[1]), int(rest[2]), rest[0], (int(rest[11]) + int(rest[12])) / takt, zeile
            except (OSError, IndexError, ValueError):
                continue

    def in_gruppe(self, pgid: int) -> list[int]:
        return [p for p, g in self.gruppe.items() if g == pgid]

    def mit_nachfahren(self, wurzel: int) -> list[int]:
        kinder: dict[int, list[int]] = {}
        for p, e in self.eltern.items():
            kinder.setdefault(e, []).append(p)
        ergebnis, offen = [], [wurzel]
        while offen:
            p = offen.pop()
            if p in self.eltern or p == wurzel:
                ergebnis.append(p)
                offen.extend(kinder.get(p, []))
        return ergebnis


# ── Programmname ─────────────────────────────────────────────────────────
PAKET = re.compile(r"\.app/Contents/MacOS/(.+?)(?:\s+-|$)")
INTERPRETER = ("python", "node")
MIT_WERT = {"-X", "-W", "-Q", "-r", "--require", "--import", "--loader"}     # Optionen der Interpreter mit eigenem Wert


def _skriptname(interpreter: str, argumente: list[str]) -> str:
    """Bei Python und Node: das Modul (-m) oder das Skript (erstes Argument, das keine Option ist); sonst der
    Interpreter selbst (etwa bei -c)."""
    i = 0
    while i < len(argumente):
        t = argumente[i]
        if t == "-m" and i + 1 < len(argumente):
            return argumente[i + 1]
        if t in ("-c", "-e", "--eval", "-p", "--print"):
            return interpreter
        if t in MIT_WERT:
            i += 2
            continue
        if not t.startswith("-"):
            return os.path.basename(t)
        i += 1
    return interpreter


def programmname(befehl: str) -> str:
    """Lesbarer Name aus der Befehlszeile: bei Python und Node das Modul (-m) oder Skript statt des Interpreters (auch
    beim Python im Paket des Mac), bei einem anderen Programm im Paket des Mac der ganze Name (auch mit Leerzeichen),
    sonst der Name der Programmdatei."""
    if paket := PAKET.search(befehl):
        teile = befehl[paket.start(1):].split()
        if teile and teile[0].lower().startswith(INTERPRETER):
            return _skriptname(teile[0], teile[1:])
        return paket.group(1).strip()
    teile = befehl.split()
    if not teile:
        return ""
    name = os.path.basename(teile[0])
    if name.lower().startswith(INTERPRETER):
        return _skriptname(name, teile[1:])
    return name


def lauschender_prozess(port: int) -> int | None:
    """Prozess, der auf dem Port lauscht (für fremd gestartete Dienste)."""
    if not MAC:
        return _lauschend_linux(port)
    try:
        ausgabe = befehl("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t")
        return int(ausgabe.split()[0]) if ausgabe.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _lauschend_linux(port: int) -> int | None:
    """Linux ohne Zusatzprogramme: Socket-Knoten aus /proc/net/tcp(6) im Zustand LISTEN, dann der Prozess,
    der ihn offen hat (nur eigene Prozesse sind lesbar, das genügt)."""
    knoten = set()
    for datei in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            zeilen = Path(datei).read_text().splitlines()[1:]
        except OSError:
            continue
        for zeile in zeilen:
            f = zeile.split()
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                knoten.add(f"socket:[{f[9]}]")
    if not knoten:
        return None
    for eintrag in Path("/proc").iterdir():
        if not eintrag.name.isdigit():
            continue
        try:
            if any(os.readlink(fd) in knoten for fd in (eintrag / "fd").iterdir()):
                return int(eintrag.name)
        except OSError:
            continue
    return None
