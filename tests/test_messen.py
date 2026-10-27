"""Messung einzelner Prozesse: Speicher (fest und eingeblendet) und lesbarer Programmname."""

from __future__ import annotations

import mmap
import os
import subprocess
import sys
import time
from pathlib import Path

from dienstkern.messen import MAC, Prozessspeicher, programmname, prozess_bytes, prozess_speicher

MIB = 1024**2


def test_programmname():
    assert programmname("/opt/homebrew/bin/caddy run --config deploy/mac/Caddyfile") == "caddy"
    assert programmname("/Users/a/.venv/bin/python -m rechtschreibung.pruefserver") == "rechtschreibung.pruefserver"
    assert programmname("/usr/bin/python3 /srv/abholer.py --takt 5") == "abholer.py"
    assert programmname("node /srv/app/server.mjs") == "server.mjs"
    assert programmname("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --type=renderer") == "Google Chrome"
    assert programmname("") == ""
    rahmen = "/opt/homebrew/Cellar/python@3.13/3.13.7/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python"
    assert programmname(f"{rahmen} -m rechtschreibung") == "rechtschreibung"
    assert programmname(f"{rahmen} /srv/befehlsdienst/.venv/bin/uvicorn --factory app:bauen") == "uvicorn"
    assert programmname("/usr/bin/python3 -X utf8 -u /srv/abholer.py") == "abholer.py"
    assert programmname("python3 -c 'print(1)'") == "python3"
    assert programmname("node") == "node"


def test_speicher_des_eigenen_prozesses():
    s = prozess_speicher(os.getpid())
    assert s.fest > 0 and prozess_bytes(os.getpid()) > 0
    assert (s.eingeblendet is not None) == MAC
    assert prozess_speicher(999_999_999) == Prozessspeicher(0, None)


def test_eingeblendete_datei_zaehlt_nicht_zum_festen_speicher(tmp_path: Path):
    """Ein Prozess blendet eine Datei von 64 MiB ein und liest sie ganz: auf dem Mac steigt der eingeblendete Anteil
    um etwa ihre Größe, der feste kaum."""
    if not MAC:
        return
    datei = tmp_path / "daten.bin"
    datei.write_bytes(os.urandom(64 * MIB))
    bereit = tmp_path / "bereit"
    skript = (f"import mmap, pathlib, time\nf = open({str(datei)!r}, 'rb')\nm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)\n"
              f"s = sum(m[i] for i in range(0, len(m), {mmap.PAGESIZE}))\npathlib.Path({str(bereit)!r}).touch()\ntime.sleep(30)\n")
    p = subprocess.Popen([sys.executable, "-c", skript])
    try:
        ende = time.monotonic() + 20
        while not bereit.exists() and time.monotonic() < ende:
            time.sleep(0.1)
        s = prozess_speicher(p.pid)
        assert s.eingeblendet is not None and s.eingeblendet >= 48 * MIB, s
        assert s.fest < 48 * MIB, s
    finally:
        p.kill()
        p.wait()
