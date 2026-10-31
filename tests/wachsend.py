"""Testdienst, der absichtlich Speicher belegt: lauscht auf einem Port (gilt damit als erreichbar) und wächst in Schritten
bis zu einer festen Obergrenze, die er nie überschreitet (die Tests bleiben weit unter dem, was der Rechner hat).

    python wachsend.py PORT SCHRITT_MB PAUSE_S HOECHSTENS_MB
"""

from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEITE = 4096


def belegen(mb: int) -> bytearray:
    """Wirklich belegt: jede Seite beschrieben, sonst zählt das System sie nicht zum Fußabdruck."""
    block = bytearray(mb * 1024 * 1024)
    block[::SEITE] = b"\x01" * len(range(0, len(block), SEITE))
    return block


def wachsen(schritt_mb: int, pause_s: float, hoechstens_mb: int, halten: list[bytearray]) -> None:
    belegt = 0
    while belegt + schritt_mb <= hoechstens_mb:
        halten.append(belegen(schritt_mb))
        belegt += schritt_mb
        time.sleep(pause_s)


class Antwort(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_: object) -> None:
        pass


if __name__ == "__main__":
    port, schritt, pause, hoechstens = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
    gehalten: list[bytearray] = []
    threading.Thread(target=wachsen, args=(schritt, pause, hoechstens, gehalten), daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), Antwort).serve_forever()
