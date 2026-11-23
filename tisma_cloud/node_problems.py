"""Why a node does not work, seen from the master: shown next to each node in the master's window and panel, never a
bare «sin señal». What the node itself sees (nothing listening, a blocked port, an unknown token) is said on its own
machine (node/connection.py); here, what the master can tell."""

from __future__ import annotations

from datetime import datetime, timezone

from . import __version__


def node_problem(enabled: bool, last_seen: datetime | None, online: bool, now: datetime,
                 version: str = "", twins: bool = False, master_version: str = __version__) -> str:
    """The reason in a sentence, or "" when the node works as it should."""
    if not enabled:
        return "Deshabilitado: no toma trabajos. Habilitalo para que vuelva."
    if last_seen is None:
        return ("Nunca se conectó. En esa PC, el diagnóstico (la ventana, o «tisma-cloud check») dice por qué: la "
                "dirección del maestro, el puerto bloqueado o el token.")
    if not online:
        minutes = max(1, round((now - last_seen).total_seconds() / 60))
        since = f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"
        return (f"Sin señal hace {since}: la PC está apagada o sin red, o su servicio está detenido. En esa PC, el "
                f"diagnóstico dice por qué.")
    if twins:
        return ("Dos programas responden con su token (un node.toml copiado, o un proceso viejo que sigue): apagá uno, "
                "o dale otro token.")
    if version and master_version and version != master_version:
        return f"Tiene la versión {version} y este maestro la {master_version}: actualizalo."
    return ""


def reconnected_text(iso: str, now: datetime | None = None) -> str:
    """«Reconectado hace N s» for a node back by itself in the last 15 minutes (the master's window), or ""."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.removesuffix("Z"))
    except ValueError:
        return ""
    seconds = int(((now or datetime.now(timezone.utc).replace(tzinfo=None)) - when).total_seconds())
    if not 0 <= seconds < 15 * 60:
        return ""
    return f"Reconectado hace {seconds} s" if seconds < 60 else f"Reconectado hace {seconds // 60} min"
