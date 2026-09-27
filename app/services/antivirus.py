from __future__ import annotations

import socket
from typing import Any

from flask import current_app

"""Varredura antivírus opcional via ClamAV (daemon clamd).

Por que existe este arquivo: nenhum modelo de linguagem detecta malware. LLM lê
texto e prevê texto — não inspeciona bytes nem conhece assinaturas. Se a
interface prometer "IA verificou que está sem vírus", a promessa é falsa.

Esta integração é opcional e degrada com clareza:
  - ANTIVIRUS_ENABLED=false (padrão): devolve status "disabled" e a interface
    diz explicitamente que não houve varredura antivírus;
  - habilitado e daemon acessível: devolve "clean" ou "infected" com a
    assinatura encontrada;
  - habilitado e daemon inacessível: devolve "unavailable" — e o chamador
    decide se bloqueia (recomendado em produção) ou apenas registra.

Para ligar, basta subir o serviço no docker-compose:

    clamav:
      image: clamav/clamav:stable
      ports: ["3310:3310"]

e definir ANTIVIRUS_ENABLED=true, ANTIVIRUS_HOST=clamav, ANTIVIRUS_PORT=3310.

Protocolo INSTREAM implementado na mão para não adicionar dependência nova:
https://docs.clamav.net/manual/Usage/Scanning.html#clamd
"""

CHUNK = 8192


def _config() -> tuple[bool, str, int, float]:
    config = current_app.config
    return (
        bool(config.get("ANTIVIRUS_ENABLED", False)),
        str(config.get("ANTIVIRUS_HOST", "clamav")),
        int(config.get("ANTIVIRUS_PORT", 3310)),
        float(config.get("ANTIVIRUS_TIMEOUT_SECONDS", 8)),
    )


def scan(raw: bytes) -> dict[str, Any]:
    enabled, host, port, timeout = _config()
    if not enabled:
        return {
            "status": "disabled",
            "engine": None,
            "signature": None,
            "detail": "Varredura antivírus não configurada neste ambiente.",
        }

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"zINSTREAM\x00")
            for start in range(0, len(raw), CHUNK):
                piece = raw[start : start + CHUNK]
                sock.sendall(len(piece).to_bytes(4, "big") + piece)
            sock.sendall((0).to_bytes(4, "big"))

            buffer = b""
            while b"\x00" not in buffer and len(buffer) < 4096:
                data = sock.recv(1024)
                if not data:
                    break
                buffer += data
    except (OSError, socket.timeout) as error:  # noqa: UP041 - compat
        return {
            "status": "unavailable",
            "engine": "clamav",
            "signature": None,
            "detail": f"Daemon ClamAV inacessível ({type(error).__name__}).",
        }

    response = buffer.split(b"\x00")[0].decode("utf-8", "replace").strip()

    if response.endswith("OK") and "FOUND" not in response:
        return {
            "status": "clean",
            "engine": "clamav",
            "signature": None,
            "detail": "Nenhuma assinatura conhecida encontrada.",
        }
    if "FOUND" in response:
        signature = response.replace("stream:", "").replace("FOUND", "").strip()
        return {
            "status": "infected",
            "engine": "clamav",
            "signature": signature,
            "detail": f"Assinatura detectada: {signature}.",
        }
    return {
        "status": "unavailable",
        "engine": "clamav",
        "signature": None,
        "detail": f"Resposta inesperada do daemon: {response[:120]}",
    }
