"""The public address of the master and the dynamic DNS (publicip.py)."""

from __future__ import annotations

import base64
import io
import urllib.error
import urllib.parse
from types import SimpleNamespace

from tisma_cloud.publicip import RETRY_S, PublicAddress, detect_ipv4, update_ddns


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_of(body: bytes | Exception, seen: list | None = None):
    def opener(request, timeout=None):  # noqa: ANN001
        if seen is not None:
            seen.append(request)
        if isinstance(body, Exception):
            raise body
        return Answer(body)
    return opener


def test_detect_ipv4():
    assert detect_ipv4("https://ip", opener=opener_of(b"1.2.3.4\n")) == "1.2.3.4"
    # Not a public IPv4: a private one, an IPv6, garbage, no answer.
    assert detect_ipv4("https://ip", opener=opener_of(b"192.168.1.5")) is None
    assert detect_ipv4("https://ip", opener=opener_of(b"2001:db8::1")) is None
    assert detect_ipv4("https://ip", opener=opener_of(b"<html>")) is None
    assert detect_ipv4("https://ip", opener=opener_of(urllib.error.URLError("down"))) is None


def test_update_ddns_request_and_answers():
    seen: list = []
    answer = update_ddns("https://dyn/nic/update", "tisma.ddns.net", "yo@x.com", "secreto", "1.2.3.4",
                         opener=opener_of(b"good 1.2.3.4", seen))
    assert answer.ok and not answer.stop
    request = seen[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert query == {"hostname": ["tisma.ddns.net"], "myip": ["1.2.3.4"]}
    assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"yo@x.com:secreto").decode()
    # No-IP: "Company NameOfProgram/OSVersion-ReleaseVersion maintainer-contact@example.com".
    agent = request.get_header("User-agent")
    assert agent.startswith("Tisma TismaCloud/") and agent.endswith(" yo@x.com")

    def answer_of(body):
        return update_ddns("https://dyn", "h", "u", "p", "1.2.3.4", opener=opener_of(body))

    assert answer_of(b"nochg 1.2.3.4").ok
    for code in (b"nohost", b"badauth", b"badagent", b"!donator", b"abuse"):
        a = answer_of(code)
        assert not a.ok and a.stop, code
    for failure in (b"911", urllib.error.URLError("down"),
                    urllib.error.HTTPError("https://dyn", 503, "busy", {}, io.BytesIO(b""))):
        a = answer_of(failure)
        assert not a.ok and not a.stop and a.retry_s == RETRY_S


def settings_of(**kw):
    base = dict(public_url="", port=8000, public_ip_url="https://ip", ddns_host="", ddns_user="", ddns_password="",
                ddns_url="https://dyn", ddns_contact="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_address_made_from_the_public_ip(tmp_path):
    ips = iter(["1.2.3.4"])
    address = PublicAddress(settings_of(), tmp_path / "state.json", detect=lambda url: next(ips))
    assert address.needed and address.url() == ""
    address.refresh(now=0.)
    assert address.url() == "http://1.2.3.4:8000"
    # TISMA_PUBLIC_URL wins, and then nothing is asked.
    fixed = PublicAddress(settings_of(public_url="https://tisma.com.uy"), tmp_path / "other.json",
                          detect=lambda url: (_ for _ in ()).throw(AssertionError("asked")))
    assert not fixed.needed and fixed.url() == "https://tisma.com.uy"
    fixed.refresh()


def test_dynamic_dns_only_when_the_ip_changes(tmp_path):
    current = ["1.2.3.4"]
    sent: list[str] = []

    def update(url, host, user, password, ip, contact):  # noqa: ANN001
        sent.append(ip)
        return SimpleNamespace(ok=True, stop=False, retry_s=0., message=f"good {ip}")

    s = settings_of(ddns_host="tisma.ddns.net", ddns_user="yo@x.com", ddns_password="p")
    path = tmp_path / "state.json"
    address = PublicAddress(s, path, detect=lambda url: current[0], update=update)
    address.refresh(now=0.)
    address.refresh(now=300.)
    assert sent == ["1.2.3.4"] and address.url() == "http://tisma.ddns.net:8000"
    current[0] = "5.6.7.8"
    address.refresh(now=600.)
    assert sent == ["1.2.3.4", "5.6.7.8"]
    # After a restart the address sent last is remembered: not sent again.
    again = PublicAddress(s, path, detect=lambda url: current[0], update=update)
    again.refresh(now=0.)
    assert sent == ["1.2.3.4", "5.6.7.8"]


def test_dynamic_dns_stops_and_waits(tmp_path):
    current = ["1.2.3.4"]
    answers = [SimpleNamespace(ok=False, stop=False, retry_s=RETRY_S, message="911"),
               SimpleNamespace(ok=False, stop=True, retry_s=0., message="badauth: usuario o contraseña incorrectos")]
    calls: list[str] = []

    def update(url, host, user, password, ip, contact):  # noqa: ANN001
        calls.append(ip)
        return answers.pop(0)

    s = settings_of(ddns_host="h", ddns_user="u", ddns_password="p")
    address = PublicAddress(s, tmp_path / "state.json", detect=lambda url: current[0], update=update)
    address.refresh(now=0.)
    # 911: not again for 30 minutes.
    address.refresh(now=RETRY_S - 1)
    assert calls == ["1.2.3.4"]
    address.refresh(now=RETRY_S + 1)
    assert calls == ["1.2.3.4", "1.2.3.4"] and address.state["ddns_error"].startswith("stop: badauth")
    # Stopped: no more updates, even when the address changes.
    current[0] = "5.6.7.8"
    address.refresh(now=RETRY_S * 3)
    assert len(calls) == 2
    # Other settings (the administrator fixed them): the stop no longer counts.
    s2 = settings_of(ddns_host="h", ddns_user="u2", ddns_password="p2")
    fixed = PublicAddress(s2, tmp_path / "state.json", detect=lambda url: current[0],
                          update=lambda *a: SimpleNamespace(ok=True, stop=False, retry_s=0., message="good"))
    assert fixed.state["ddns_error"] == ""
    fixed.refresh(now=0.)
    assert fixed.state["ddns_ip"] == "5.6.7.8"


def test_health_gives_the_automatic_address(client):
    services = client.app.state.services
    services.public_address.state["ip"] = "1.2.3.4"
    assert client.get("/v1/health").json()["billing_url"] == "http://1.2.3.4:8000/cuenta"
