"""The address of the master on internet when TISMA_PUBLIC_URL is empty: its public IPv4, asked to TISMA_PUBLIC_IP_URL
(ipify by default), or the name of a dynamic DNS service kept pointing at it (No-IP by default, or any service of the
same dyndns2 protocol), for the internet connections whose address changes (all the home and office ones in Uruguay).

No-IP update protocol (https://www.noip.com/integrate/request, https://www.noip.com/integrate/response): a GET of
/nic/update?hostname=...&myip=... with HTTP Basic authentication and a User-Agent "Company NameOfProgram/OSVersion-
ReleaseVersion maintainer-contact@example.com"; an update only when the address changed; "good" and "nochg" are
success, "nohost", "badauth", "badagent", "!donator" and "abuse" stop the updates until the administrator fixes the
settings, "911" (or an HTTP 5xx) is retried no sooner than 30 minutes later.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import platform
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

log = logging.getLogger("tisma_cloud.publicip")

# How often the public address is checked [s].
CHECK_S = 300
# No-IP: "Retry the update no sooner than 30 minutes" after a 911.
RETRY_S = 30 * 60
# No-IP answers that need the administrator (wrong settings or a blocked client): no more updates until then.
STOP_CODES = {
    "nohost": "el nombre no existe en esa cuenta",
    "badauth": "usuario o contraseña incorrectos",
    "badagent": "el servicio deshabilitó este cliente",
    "!donator": "la cuenta no tiene esa función",
    "abuse": "la cuenta está bloqueada por abuso",
}


def detect_ipv4(url: str, timeout: float = 10., opener=urllib.request.urlopen) -> str | None:
    """The public IPv4 of this machine as seen from internet (the service answers it as plain text), or None when it
    does not answer or the answer is not a public IPv4."""
    request = urllib.request.Request(url, headers={"User-Agent": f"TismaCloud/{__version__}"})
    try:
        with opener(request, timeout=timeout) as r:
            text = r.read(64).decode("ascii", "replace").strip()
        ip = ipaddress.IPv4Address(text)
    except (OSError, ValueError):
        return None
    return str(ip) if ip.is_global else None


@dataclass
class DdnsAnswer:
    ok: bool
    # The updates must stop until the administrator changes the settings.
    stop: bool = False
    # Seconds before the next attempt (a failure that may pass).
    retry_s: float = 0.
    message: str = ""


def update_ddns(url: str, host: str, user: str, password: str, ip: str, contact: str = "",
                opener=urllib.request.urlopen) -> DdnsAnswer:
    """Points the name of a dynamic DNS service (dyndns2 protocol) at this IPv4."""
    query = urllib.parse.urlencode({"hostname": host, "myip": ip})
    auth = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    agent = f"Tisma TismaCloud/{platform.system()}-{__version__} {contact or user}"
    request = urllib.request.Request(f"{url}?{query}", headers={"Authorization": f"Basic {auth}", "User-Agent": agent})
    try:
        with opener(request, timeout=20) as r:
            body = r.read(512).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            return DdnsAnswer(False, retry_s=RETRY_S, message=f"error del servicio (HTTP {e.code})")
        body = e.read(512).decode("utf-8", "replace").strip()
    except OSError as e:
        return DdnsAnswer(False, retry_s=RETRY_S, message=f"sin conexión con el servicio ({e})")
    code = body.split()[0] if body else ""
    if code in ("good", "nochg"):
        return DdnsAnswer(True, message=body)
    if code in STOP_CODES:
        return DdnsAnswer(False, stop=True, message=f"{code}: {STOP_CODES[code]}")
    if code == "911":
        return DdnsAnswer(False, retry_s=RETRY_S, message="911: falla del servicio")
    return DdnsAnswer(False, retry_s=RETRY_S, message=f"respuesta desconocida: {body[:80]!r}")


class PublicAddress:
    """The public address of the master and the dynamic DNS name, checked every CHECK_S by the master. Its state is kept
    in a small file of the storage folder, read by the window of the server and kept after a restart (so that an
    unchanged address is not sent again: No-IP blocks the clients that update without a change)."""

    def __init__(self, settings, state_path: Path, *, detect=detect_ipv4, update=update_ddns) -> None:
        self.settings = settings
        self.state_path = Path(state_path)
        self._detect, self._update = detect, update
        self._lock = threading.Lock()
        self.state = {"ip": "", "ddns_ip": "", "ddns_error": "", "checked_at": ""}
        try:
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        # A stop answer of an earlier run with other settings does not count: the settings may have been fixed.
        if self.state.get("ddns_settings") != self._ddns_settings():
            self.state["ddns_error"] = ""
        self._retry_at = 0.

    def _ddns_settings(self) -> str:
        s = self.settings
        return f"{s.ddns_url}|{s.ddns_host}|{s.ddns_user}"

    @property
    def ddns_enabled(self) -> bool:
        s = self.settings
        return bool(s.ddns_host and s.ddns_user and s.ddns_password)

    @property
    def needed(self) -> bool:
        """Something to do: no public address set (and a service to ask it), or a dynamic DNS name to keep."""
        return bool(self.settings.public_ip_url) and (not self.settings.public_url or self.ddns_enabled)

    def url(self) -> str:
        """TISMA_PUBLIC_URL, else http:// the dynamic DNS name or the public IPv4, and the port of the master."""
        if self.settings.public_url:
            return self.settings.public_url
        host = self.settings.ddns_host or self.state.get("ip", "")
        return f"http://{host}:{self.settings.port}" if host else ""

    def refresh(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if not self.needed:
            return
        with self._lock:
            ip = self._detect(self.settings.public_ip_url)
            if ip is None:
                log.warning("No public IPv4 from %s", self.settings.public_ip_url)
                return
            if ip != self.state.get("ip"):
                log.info("Public IPv4: %s", ip)
            self.state["ip"] = ip
            self.state["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if (self.ddns_enabled and not self.state.get("ddns_error", "").startswith("stop:") and now >= self._retry_at
                    and ip != self.state.get("ddns_ip")):
                s = self.settings
                answer = self._update(s.ddns_url, s.ddns_host, s.ddns_user, s.ddns_password, ip,
                                      s.ddns_contact or "")
                if answer.ok:
                    log.info("Dynamic DNS %s -> %s (%s)", s.ddns_host, ip, answer.message)
                    self.state["ddns_ip"], self.state["ddns_error"] = ip, ""
                elif answer.stop:
                    log.error("Dynamic DNS %s stopped: %s", s.ddns_host, answer.message)
                    self.state["ddns_error"] = f"stop: {answer.message}"
                else:
                    log.warning("Dynamic DNS %s: %s; again in %d min", s.ddns_host, answer.message,
                                answer.retry_s // 60)
                    self.state["ddns_error"] = answer.message
                    self._retry_at = now + answer.retry_s
            self.state["ddns_settings"] = self._ddns_settings()
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
            except OSError:
                log.exception("Writing %s failed", self.state_path)
