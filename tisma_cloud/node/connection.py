"""Why a node does not reach its master, in words for the person at the machine (the node's status, its window, the
diagnosis): never a bare «Error». Each failure of a request is told apart:

- nothing listens at that address and port (connection refused): the master is stopped, or the port is another;
- nothing answers in time (connection timeout): a firewall drops the packets (the master's port is not open), or the
  address is not the master's in this network;
- the name of the master is not found, or this machine has no route to it;
- the HTTPS certificate is not accepted;
- the master answers but does not know this node's token (it was removed), or has it disabled;
- something answers that is not a Tisma Cloud master;
- the master runs another version than this node.

The node program notes how its last request went in node-state.json (in the folder of the service), so that «status»
and the window say it while the service runs."""

from __future__ import annotations

import errno
import json
import os
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .errors import HTTPStatusError, _both

ConnectTimeout = _both("ConnectTimeout")
ReadTimeout = _both("ReadTimeout")

STATE_FILE = "node-state.json"

# getaddrinfo: the name does not exist (NXDOMAIN, or a name with no address): POSIX EAI_NONAME, EAI_NODATA; Windows
# WSAHOST_NOT_FOUND, WSANO_DATA. And the DNS could not be asked now (no network yet, a DNS that does not answer):
# EAI_AGAIN; Windows WSATRY_AGAIN.
DNS_NO_NAME = {getattr(socket, "EAI_NONAME", -2), getattr(socket, "EAI_NODATA", -5), 11001, 11004}
DNS_NOT_NOW = {getattr(socket, "EAI_AGAIN", -3), 11002}

# Error numbers of an unreachable network or machine (POSIX and Windows' WSAENETUNREACH, WSAEHOSTUNREACH).
UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}


def _where(master_url: str) -> str:
    parts = urlsplit(master_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.hostname or master_url}:{port}"


def _causes(ex: BaseException):
    seen = set()
    while ex is not None and id(ex) not in seen:
        seen.add(id(ex))
        yield ex
        ex = ex.__cause__ or ex.__context__


def explain(ex: BaseException, master_url: str) -> str:
    """A failure of a request to the master, said for a person."""
    where = _where(master_url)
    host = urlsplit(master_url).hostname or master_url
    if isinstance(ex, HTTPStatusError):
        return explain_status(ex.response.status_code, master_url)
    for cause in _causes(ex):
        if isinstance(cause, (ssl.SSLError, ssl.CertificateError)):
            return (f"El certificado HTTPS de {host} no es válido para este nodo: revisá la dirección del maestro, o "
                    f"«verify_tls» si el maestro usa uno propio")
        if isinstance(cause, socket.gaierror):
            if cause.errno in DNS_NO_NAME:
                return (f"{host} no existe en el DNS: revisá el dominio (o la dirección del maestro, master_url) "
                        f"[{cause}]")
            if cause.errno in DNS_NOT_NOW:
                return (f"No se pudo consultar el DNS por {host}: esta máquina no tiene red todavía, o su DNS no "
                        f"responde; se vuelve a intentar [{cause}]")
            return f"No se encuentra «{host}» en el DNS: revisá la dirección del maestro (master_url) [{cause}]"
        if isinstance(cause, ConnectionRefusedError):
            return (f"Nada atiende en {where}: el maestro está detenido, o su puerto es otro (en el maestro: "
                    f"«tisma-cloud status» dice el puerto)")
        if isinstance(cause, (*ConnectTimeout, TimeoutError, socket.timeout)):
            return (f"El maestro no contesta en {where}: su puerto está bloqueado (en el maestro: «tisma-cloud firewall "
                    f"on»), o esa no es su dirección en esta red")
        if isinstance(cause, ReadTimeout):
            return f"El maestro en {where} tardó demasiado en contestar (¿muy ocupado?); se vuelve a intentar"
        if isinstance(cause, OSError) and (cause.errno in UNREACHABLE or getattr(cause, "winerror", None) in UNREACHABLE):
            return f"Esta máquina no llega a {host}: no tiene red, o la dirección es de otra red"
    return f"No se pudo hablar con el maestro en {where} ({type(ex).__name__}: {ex})"


def explain_status(code: int, master_url: str) -> str:
    """An answer of the master that is not the expected one, said for a person."""
    if code == 401:
        return ("El maestro no reconoce el token de este nodo (lo quitaron, o es de otro maestro): el nodo pide unirse "
                "de nuevo; aceptalo en la ventana del maestro (Nodos)")
    if code == 403:
        return "El maestro tiene este nodo deshabilitado: habilitalo en la ventana del maestro (Nodos)"
    if code == 404:
        return f"En {master_url} responde otro programa, no un maestro de Tisma Cloud: revisá la dirección y el puerto"
    if code == 429:
        return "El maestro pide esperar (demasiados pedidos seguidos); se vuelve a intentar"
    if code >= 500:
        return f"El maestro tuvo un problema ({code}); se vuelve a intentar"
    return f"El maestro contestó {code}"


def version_note(master_version: str, node_version: str) -> str | None:
    """A note when the master and this node are of different versions (they still work together; better updated)."""
    if master_version and node_version and master_version != node_version:
        return (f"El maestro tiene la versión {master_version} y este nodo la {node_version}: actualizá el que esté "
                f"atrás (primero el maestro, después los nodos)")
    return None


class StateFile:
    """How the node's last request to the master went, written only when it changes (not at every request)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._last: tuple[bool, str] | None = None

    def write(self, ok: bool, message: str) -> None:
        if self._last == (ok, message):
            return
        self._last = (ok, message)
        data = {"ok": ok, "message": message, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "pid": os.getpid()}
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def read_state(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
