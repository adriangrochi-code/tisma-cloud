"""The user's address behind a proxy: X-Forwarded-For behind Caddy, CF-Connecting-IP behind a Cloudflare Tunnel."""
from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from conftest import Factory, new_app


def _admin_status(client: TestClient, headers: dict[str, str]) -> int:
    return client.get("/v1/admin/nodes", headers=headers).status_code


def test_tunnel_ignores_a_forged_forwarded_for(settings):
    # Behind a Cloudflare Tunnel every request reaches the master from cloudflared on the same machine.
    tunnel = dataclasses.replace(settings, client_ip_header="cf-connecting-ip")
    with TestClient(new_app(tunnel), client=("127.0.0.1", 50000)) as c:
        f = Factory(c)
        admin = f.user("root@x.com", admin=True)
        # From internet: Cloudflare sets CF-Connecting-IP and appends to whatever X-Forwarded-For the user sent.
        forged = {**admin, "x-forwarded-for": "192.168.1.5, 203.0.113.9", "cf-connecting-ip": "203.0.113.9"}
        assert _admin_status(c, forged) == 404
        # Only CF-Connecting-IP with a public address (the setting as the guide writes it, any case): still 404.
        assert _admin_status(c, {**admin, "cf-connecting-ip": "203.0.113.9"}) == 404
        # A computer of the local network reaching port 8000 directly carries no Cloudflare header.
        assert _admin_status(c, admin) == 200


def test_caddy_keeps_using_forwarded_for(settings):
    # The default: behind Caddy, which replaces X-Forwarded-For with the user's address.
    with TestClient(new_app(settings), client=("127.0.0.1", 50000)) as c:
        f = Factory(c)
        admin = f.user("root@x.com", admin=True)
        assert _admin_status(c, {**admin, "x-forwarded-for": "203.0.113.9"}) == 404
        assert _admin_status(c, {**admin, "x-forwarded-for": "192.168.1.5"}) == 200


def test_forwarded_for_from_internet_is_ignored(settings):
    # Port 8000 forwarded in the router by mistake: a connection from internet straight to the master writes the local
    # network in X-Forwarded-For. The header counts only from a proxy (TISMA_TRUSTED_PROXIES, the local network).
    with TestClient(new_app(settings), client=("203.0.113.9", 50000)) as c:
        f = Factory(c)
        admin = f.user("root@x.com", admin=True)
        assert _admin_status(c, {**admin, "x-forwarded-for": "192.168.1.5"}) == 404
        assert _admin_status(c, {**admin, "cf-connecting-ip": "192.168.1.5"}) == 404


def test_trusted_proxies_can_be_narrowed(settings):
    # Only cloudflared on this machine: a computer of the local network cannot pick another address with the header.
    only_here = dataclasses.replace(settings, client_ip_header="cf-connecting-ip", trusted_proxies="127.0.0.0/8")
    with TestClient(new_app(only_here), client=("192.168.1.7", 50000)) as c:
        f = Factory(c)
        admin = f.user("root@x.com", admin=True)
        assert _admin_status(c, {**admin, "cf-connecting-ip": "203.0.113.9"}) == 200
    with TestClient(new_app(only_here), client=("127.0.0.1", 50000)) as c:
        f = Factory(c)
        admin = f.user("root2@x.com", admin=True)
        assert _admin_status(c, {**admin, "cf-connecting-ip": "203.0.113.9"}) == 404
