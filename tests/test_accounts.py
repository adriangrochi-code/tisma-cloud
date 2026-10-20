from __future__ import annotations

import re
import time

import jwt
import pytest
from conftest import new_app as create_app
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from tisma_cloud.google_auth import GoogleVerifier
from tisma_cloud.mail import SmtpMailer

INTERNET = ("200.40.10.7", 40000)
CLIENT_ID = "123-web.apps.googleusercontent.com"


def code_of(c, email: str, purpose_word: str = "") -> str:
    sent = [m for m in c.app.state.services.mailer.sent if m[0] == email and purpose_word in m[1]]
    return re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)


@pytest.fixture
def web(settings):
    settings.login_per_minute = 100
    app = create_app(settings)
    with TestClient(app, client=INTERNET) as c:
        yield c


def test_register_confirm_and_login(web):
    r = web.post("/v1/auth/register", json={"email": "Lu@correo.uy", "password": "una clave larga"})
    assert r.status_code == 201 and r.json() == {"verification_required": True, "email": "lu@correo.uy"}
    to, subject, text = web.app.state.services.mailer.sent[-1]
    assert to == "lu@correo.uy" and "confirmar la cuenta" in subject
    # Until it is confirmed, the password does not open a session.
    r = web.post("/v1/auth/login", json={"email": "lu@correo.uy", "password": "una clave larga"})
    assert r.status_code == 403 and "not verified" in r.json()["detail"]
    assert web.post("/v1/auth/verify", json={"email": "lu@correo.uy", "code": "000000"}).status_code in (400,)
    code = code_of(web, "lu@correo.uy")
    r = web.post("/v1/auth/verify", json={"email": "lu@correo.uy", "code": code})
    assert r.status_code == 200 and r.json()["token"].startswith("tu_")
    # A code works once.
    assert web.post("/v1/auth/verify", json={"email": "lu@correo.uy", "code": code}).status_code == 400
    assert web.post("/v1/auth/login", json={"email": "lu@correo.uy", "password": "una clave larga"}).status_code == 200


def test_codes_die_after_five_wrong_attempts(web):
    web.post("/v1/auth/register", json={"email": "a@correo.uy", "password": "una clave larga"})
    code = code_of(web, "a@correo.uy")
    wrong = "%06d" % ((int(code) + 1) % 1_000_000)
    for _ in range(5):
        assert web.post("/v1/auth/verify", json={"email": "a@correo.uy", "code": wrong}).status_code == 400
    assert web.post("/v1/auth/verify", json={"email": "a@correo.uy", "code": code}).status_code == 400
    assert web.post("/v1/auth/verify", json={"email": "a@correo.uy", "code": "12ab56"}).status_code == 422


def test_resend_and_forgot_do_not_tell_who_has_an_account(web):
    sent = web.app.state.services.mailer.sent
    n = len(sent)
    assert web.post("/v1/auth/resend", json={"email": "nadie@correo.uy"}).json() == {"ok": True}
    assert web.post("/v1/auth/password/forgot", json={"email": "nadie2@correo.uy"}).json() == {"ok": True}
    assert len(sent) == n
    # One code per minute to an address.
    web.post("/v1/auth/register", json={"email": "b@correo.uy", "password": "una clave larga"})
    assert web.post("/v1/auth/resend", json={"email": "b@correo.uy"}).status_code == 200
    assert web.post("/v1/auth/resend", json={"email": "b@correo.uy"}).status_code == 429


def test_new_password_with_a_code(web):
    web.post("/v1/auth/register", json={"email": "c@correo.uy", "password": "una clave larga"})
    first = web.post("/v1/auth/verify", json={"email": "c@correo.uy", "code": code_of(web, "c@correo.uy")}).json()["token"]
    assert web.post("/v1/auth/password/forgot", json={"email": "c@correo.uy"}).status_code == 200
    code = code_of(web, "c@correo.uy", "contraseña")
    r = web.post("/v1/auth/password/reset", json={"email": "c@correo.uy", "code": code, "password": "clave nueva 123"})
    assert r.status_code == 200
    # The old sessions are closed; the new password works and the old one not.
    assert web.get("/v1/me", headers={"Authorization": f"Bearer {first}"}).status_code == 401
    assert web.post("/v1/auth/login", json={"email": "c@correo.uy", "password": "clave nueva 123"}).status_code == 200
    assert web.post("/v1/auth/login", json={"email": "c@correo.uy", "password": "una clave larga"}).status_code == 401


def test_smtp_mailer_sends_through_a_real_smtp_server():
    from aiosmtpd.controller import Controller

    received = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):  # noqa: N802
            received.append((envelope.mail_from, envelope.rcpt_tos, envelope.content.decode()))
            return "250 OK"

    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ctl = Controller(Handler(), hostname="127.0.0.1", port=port)
    ctl.start()
    try:
        SmtpMailer("127.0.0.1", port, "none", "", "", "Tisma Cloud <noreply@tisma.com.uy>").send(
            "lu@correo.uy", "Tu código", "Tu código es 123456")
    finally:
        ctl.stop()
    assert received[0][0] == "noreply@tisma.com.uy" and received[0][1] == ["lu@correo.uy"]
    assert "123456" in received[0][2] and "Subject: Tu =?utf-8?q?c=C3=B3digo?=" in received[0][2]


# ---- Sign in with Google: tokens signed with our own key standing in for Google's.
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    class _Key:
        key = KEY.public_key()

    def get_signing_key_from_jwt(self, token):
        return self._Key()


def google_token(email: str, sub: str = "g-1", verified: bool = True, hd: str | None = None, aud: str = CLIENT_ID,
                 iss: str = "https://accounts.google.com", exp: float | None = None, key=KEY) -> str:
    claims = {"iss": iss, "aud": aud, "sub": sub, "email": email, "email_verified": verified,
              "iat": int(time.time()), "exp": int(exp or time.time() + 600)}
    if hd:
        claims["hd"] = hd
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def gweb(web):
    web.app.state.services.google = GoogleVerifier([CLIENT_ID], jwk_client=FakeJwks())
    return web


def test_google_sign_in(gweb):
    # A Gmail address: account created and confirmed by Google.
    r = gweb.post("/v1/auth/google", json={"id_token": google_token("Maria@gmail.com"), "device": "Teléfono"})
    assert r.status_code == 200 and r.json()["email"] == "maria@gmail.com"
    me = gweb.get("/v1/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()
    assert me["email"] == "maria@gmail.com" and me["paid"] is False
    # The same Google account again: same user.
    assert gweb.post("/v1/auth/google", json={"id_token": google_token("maria@gmail.com")}).json()["email"] == "maria@gmail.com"
    # An address Google does not vouch for (not Gmail, no Workspace): our code confirms it.
    r = gweb.post("/v1/auth/google", json={"id_token": google_token("pepe@correo.uy", sub="g-2")})
    assert r.json() == {"verification_required": True, "email": "pepe@correo.uy"}
    assert gweb.post("/v1/auth/verify", json={"email": "pepe@correo.uy", "code": code_of(gweb, "pepe@correo.uy")}).status_code == 200


def test_google_links_only_when_google_vouches_for_the_address(gweb):
    gweb.post("/v1/auth/register", json={"email": "ana@gmail.com", "password": "una clave larga"})
    gweb.post("/v1/auth/register", json={"email": "ana@empresa.uy", "password": "una clave larga"})
    # Gmail: Google is the authority, the existing account is linked (and confirmed).
    r = gweb.post("/v1/auth/google", json={"id_token": google_token("ana@gmail.com", sub="g-ana")})
    assert r.status_code == 200
    assert gweb.post("/v1/auth/login", json={"email": "ana@gmail.com", "password": "una clave larga"}).status_code == 200
    # Another domain without Workspace: refused, the account keeps its password only.
    r = gweb.post("/v1/auth/google", json={"id_token": google_token("ana@empresa.uy", sub="g-x")})
    assert r.status_code == 409
    # Workspace of that domain with a verified address: linked.
    r = gweb.post("/v1/auth/google", json={"id_token": google_token("ana@empresa.uy", sub="g-y", hd="empresa.uy")})
    assert r.status_code == 200


def test_google_tokens_are_checked(gweb):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = [google_token("x@gmail.com", aud="otra-app"), google_token("x@gmail.com", iss="https://evil.example"),
           google_token("x@gmail.com", exp=time.time() - 3600), google_token("x@gmail.com", key=other), "abc.def.ghi" * 3]
    for token in bad:
        assert gweb.post("/v1/auth/google", json={"id_token": token}).status_code == 401, token[:20]


def test_google_disabled_without_client_id(web):
    assert web.get("/v1/health").json()["google_client_id"] == ""
    assert web.post("/v1/auth/google", json={"id_token": google_token("x@gmail.com")}).status_code == 401
