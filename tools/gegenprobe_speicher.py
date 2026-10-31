"""Gegenprobe zur Speicherwache: ein Dienst, angemeldet mit 0,2 GB, wächst in Schritten von 100 MB bis 1,2 GB (nie mehr).
Läuft mit dem Dienstkern, der gerade importierbar ist, die Schleife des Verwalters zehn Sekunden lang und meldet, ob der
Dienst über 0,5 GB (Grenze 0,2 * 1,5 + 0,2) weiterlief. Endet mit 1 (rot), wenn er weiterlief.

    PYTHONPATH=<ordner mit dienstkern> python tools/gegenprobe_speicher.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
from pathlib import Path

import dienstkern
from dienstkern.verwalter import Neustartregeln, Verwalter
from dienstkern.vorlage import Vorlage, dateien_lesen

WACHSEND = Path(__file__).resolve().parent.parent / "tests" / "wachsend.py"


def freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Probe(Verwalter):
    @property
    def speicherschutz(self):
        try:
            from dienstkern.speicherwache import Speicherschutz
        except ImportError:                       # Fassung ohne Speicherwache
            return None
        return Speicherschutz(takt_s=0.2, faktor=1.5, zuschlag_gb=0.2, frei_mindestens_prozent=0)


async def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        port = freier_port()
        (tmp / "dienste").mkdir()
        (tmp / "dienste" / "hoerer.toml").write_text(
            f'name = "Wächst"\nordner = "{tmp}"\nbefehl = ["{sys.executable}", "{WACHSEND}", "{port}", "100", "0.1", "1200"]\n'
            f"port = {port}\nspeicher_gb = 0.2\nstartzeit_s = 15\nanhaltezeit_s = 5\n")
        v = Probe(lambda: dateien_lesen(tmp / "dienste", Vorlage), tmp / "protokolle", tmp / "zustand.json",
                  Neustartregeln(pausen_s=[0.2]), takt_s=0.5, herkunft="von der Gegenprobe")
        schleife = asyncio.create_task(v.schleife())
        await asyncio.sleep(1)
        await v.starten("hoerer")
        hoechst = 0.0
        for _ in range(20):
            await asyncio.sleep(0.5)
            gb = v.dienste["hoerer"].gb or 0.0
            hoechst = max(hoechst, gb)
        laeuft = v.dienste["hoerer"].stand.gruppe is not None
        await v.anhalten("hoerer")
        schleife.cancel()
        print(f"Dienstkern {dienstkern.__version__}: höchstens {hoechst:.2f} GB gemessen, läuft nach 10 s {'noch' if laeuft else 'nicht mehr'}")
        rot = laeuft and hoechst > 0.5
        print("ROT: über der Grenze weitergelaufen" if rot else "GRÜN: beendet")
        return 1 if rot else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
