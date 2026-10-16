"""Prozessmessung mit Bordmitteln des Systems: Prozesstabelle (Gruppen, Nachfahren, Rechenzeit), Speicher eines
Prozesses und der Prozess hinter einem lauschenden Port.

Prozessspeicher: auf dem Mac der physische Fußabdruck (wie die Aktivitätsanzeige; RSS zählt geteilte Seiten falsch),
auf Linux PSS. Einheit überall GiB, wie die Systemanzeigen.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import re
import subprocess
from pathlib import Path

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


def prozess_bytes(pid: int) -> int:
    """Speicher eines Prozesses in Byte; 0, wenn er nicht (mehr) lesbar ist."""
    if MAC:
        info = _RusageV0()
        if _libproc.proc_pid_rusage(pid, 0, ctypes.byref(info)) != 0:
            return 0
        return int(info.phys_footprint)
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
