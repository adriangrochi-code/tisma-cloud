"""Security and privacy of the accounts: password hashes, sessions, a copy of the data, deletion, what is forgotten."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import timedelta

from tisma_cloud import crypto, security
from tisma_cloud.models import ApiToken, Bridge, Job, Payment, RemoteCommand, User, utcnow
from tisma_cloud.security import hash_password, needs_rehash, verify_password


def with_password(f, email: str, password: str = "una clave larga", stored: str | None = None) -> dict:
    headers = f.user(email)
    with f.db.session() as s:
        user = s.query(User).filter_by(email_index=crypto.index(email)).one()
        user.password_hash = stored or hash_password(password)
    return headers


def test_passwords_hashed_as_owasp_recommends(monkeypatch):
    monkeypatch.setattr(security, "_N", 1 << 17)
    stored = hash_password("una clave larga")
    # scrypt N=2^17 (128 MiB), r=8, p=1: the first setting of the OWASP Password Storage Cheat Sheet.
    assert stored.startswith("scrypt$131072$8$1$") and not needs_rehash(stored)
    assert verify_password("una clave larga", stored) and not verify_password("otra", stored)
    # A hash of the old parameters still opens, and is to be made again.
    old = stored.replace("scrypt$131072$", "scrypt$16384$")
    assert needs_rehash(old)
    # Parameters beyond the current ones are refused (a planted hash cannot make the server use more memory).
    assert not verify_password("x", stored.replace("$131072$", "$1048576$"))


def test_old_hashes_are_made_again_at_login(client, f):
    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(b"una clave larga", salt=salt, n=1 << 10, r=8, p=1, dklen=32)
    old = f"scrypt$1024$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    with_password(f, "vieja@x.com", stored=old)
    assert client.post("/v1/auth/login", json={"email": "vieja@x.com", "password": "una clave larga"}).status_code == 200
    with f.db.session() as s:
        stored = s.query(User).filter_by(email_index=crypto.index("vieja@x.com")).one().password_hash
    assert stored != old and not needs_rehash(stored) and verify_password("una clave larga", stored)


def test_too_many_passwords_at_once_wait_instead_of_eating_memory(client, monkeypatch):
    monkeypatch.setattr(security, "_WAIT_S", 0.1)
    for _ in range(2):
        security._SLOTS.acquire()
    try:
        r = client.post("/v1/auth/register", json={"email": "z@x.com", "password": "una clave larga"})
        assert r.status_code == 503 and r.headers["retry-after"]
    finally:
        for _ in range(2):
            security._SLOTS.release()


def test_sessions_listed_closed_and_expired(client, f):
    first = with_password(f, "ana@x.com")
    second = client.post("/v1/auth/login", json={"email": "ana@x.com", "password": "una clave larga",
                                                 "device": "Teléfono"}).json()["token"]
    second = {"Authorization": f"Bearer {second}"}
    sessions = client.get("/v1/me/sessions", headers=second).json()["sessions"]
    assert len(sessions) == 2 and [x["device"] for x in sessions if x["current"]] == ["Teléfono"]
    # A lost device is closed from another one.
    other = next(x["id"] for x in sessions if not x["current"])
    assert client.delete(f"/v1/me/sessions/{other}", headers=second).json() == {"ok": True}
    assert client.get("/v1/me", headers=first).status_code == 401
    # Not another account's.
    bruno = f.user("bruno@x.com")
    assert client.delete(f"/v1/me/sessions/{sessions[0]['id']}", headers=bruno).status_code == 404
    # A session unused for session_days ends.
    with f.db.session() as s:
        for t in s.query(ApiToken).all():
            t.last_used = utcnow() - timedelta(days=91)
    r = client.get("/v1/me", headers=second)
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    client.app.state.services.forget_old_data()
    with f.db.session() as s:
        assert s.query(ApiToken).count() == 0


def test_close_the_other_sessions_and_change_the_password(client, f):
    keep = with_password(f, "ana@x.com")
    others = [client.post("/v1/auth/login", json={"email": "ana@x.com", "password": "una clave larga"}).json()["token"]
              for _ in range(2)]
    assert client.post("/v1/me/sessions/close-others", headers=keep).json() == {"closed": 2}
    assert all(client.get("/v1/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401 for t in others)
    other = {"Authorization": "Bearer " + client.post("/v1/auth/login", json={
        "email": "ana@x.com", "password": "una clave larga"}).json()["token"]}
    body = {"current_password": "mal", "new_password": "clave nueva 123"}
    assert client.post("/v1/me/password", headers=keep, json=body).status_code == 403
    body["current_password"] = "una clave larga"
    assert client.post("/v1/me/password", headers=keep, json=body).json() == {"ok": True}
    # The other devices are logged out; this one goes on; the new password opens, the old one not.
    assert client.get("/v1/me", headers=other).status_code == 401 and client.get("/v1/me", headers=keep).status_code == 200
    assert client.post("/v1/auth/login", json={"email": "ana@x.com", "password": "clave nueva 123"}).status_code == 200
    assert client.post("/v1/auth/login", json={"email": "ana@x.com", "password": "una clave larga"}).status_code == 401


def test_a_copy_of_everything_kept(client, f):
    ana = with_password(f, "ana@x.com")
    f.submit(ana)
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "PC", "printers": [{"id": "p", "name": "MK4"}]})
    r = client.get("/v1/me/export", headers=ana)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["account"]["email"] == "ana@x.com" and data["account"]["has_password"]
    assert len(data["jobs"]) == 1 and data["bridges"][0]["printers"][0]["name"] == "MK4"
    assert len(data["sessions"]) == 1
    # No secrets in it.
    assert "scrypt" not in r.text and "token" not in r.text.lower()


def test_delete_the_account(client, f):
    ana = with_password(f, "ana@x.com")
    services = client.app.state.services
    job = f.submit(ana)
    client.put("/v1/bridges/pc-1", headers=ana, json={"name": "PC", "printers": [{"id": "p", "name": "MK4",
                                                                                  "actions": ["pause"]}]})
    client.get("/v1/bridges/pc-1/commands?wait=0", headers=ana)
    command = client.post("/v1/remote/bridges/pc-1/printers/p/pause", headers=ana).json()
    with f.db.session() as s:
        user = s.query(User).filter_by(email_index=crypto.index("ana@x.com")).one()
        s.add(Payment(user_id=user.id, plan_name=user.plan_name, amount_cents=200, provider="manual", status="approved"))
        user_id = user.id
    assert client.post("/v1/me/delete", headers=ana, json={"password": "mal"}).status_code == 403
    assert client.post("/v1/me/delete", headers=ana, json={"password": "una clave larga"}).json() == {"ok": True}
    # Nothing personal is left: sessions (also the ones in memory), jobs and their files, bridges, commands.
    assert client.get("/v1/me", headers=ana).status_code == 401
    assert client.get("/v1/bridges/pc-1/commands?wait=0", headers=ana).status_code == 401
    assert not services.storage.job_dir(job["id"]).exists()
    with f.db.session() as s:
        user = s.get(User, user_id)
        assert user.email == f"deleted-{user_id}@deleted.invalid" and not user.enabled and user.password_hash == ""
        assert s.query(Job).count() == 0 and s.query(Bridge).count() == 0
        assert s.get(RemoteCommand, command["id"]) is None
        # The payments stay, for the accounting, tied to nobody.
        assert s.query(Payment).filter_by(user_id=user_id).count() == 1
    # The address can make a new account.
    assert client.post("/v1/auth/register", json={"email": "ana@x.com", "password": "una clave larga"}).status_code == 201


def test_delete_an_account_without_password_with_a_code(client, f):
    eva = f.user("eva@x.com")
    sent = client.app.state.services.mailer.sent
    assert client.post("/v1/me/delete", headers=eva, json={}).status_code == 403
    assert client.post("/v1/me/delete/code", headers=eva).json() == {"ok": True}
    to, subject, text = sent[-1]
    assert to == "eva@x.com" and "borrar" in subject
    code = re.search(r"\b(\d{6})\b", text).group(1)
    assert client.post("/v1/me/delete", headers=eva, json={"code": code}).json() == {"ok": True}
    assert client.get("/v1/me", headers=eva).status_code == 401


def test_registering_a_known_address_does_not_tell_it(client, f):
    with_password(f, "ana@x.com")
    sent = client.app.state.services.mailer.sent
    new = client.post("/v1/auth/register", json={"email": "nuevo@x.com", "password": "una clave larga"})
    known = client.post("/v1/auth/register", json={"email": "ana@x.com", "password": "otra clave larga"})
    # The same answer; the owner of the address is told by e-mail; the account does not change.
    assert new.status_code == known.status_code == 201 and new.json().keys() == known.json().keys()
    assert sent[-1][0] == "ana@x.com" and "Ya tenés una cuenta" in sent[-1][1]
    assert client.post("/v1/auth/login", json={"email": "ana@x.com", "password": "una clave larga"}).status_code == 200
    with f.db.session() as s:
        assert s.query(User).filter_by(email_index=crypto.index("ana@x.com")).count() == 1


def test_old_data_is_forgotten(client, f):
    ana = with_password(f, "ana@x.com")
    services = client.app.state.services
    job = f.submit(ana)
    old = utcnow() - timedelta(days=91)
    with f.db.session() as s:
        j = s.get(Job, job["id"])
        j.status, j.finished_at, j.files_removed = "done", old, True
        user_id = j.user_id
        s.add(Bridge(id="viejo", user_id=user_id, last_seen=utcnow() - timedelta(days=200)))
        s.add(Bridge(id="nuevo", user_id=user_id, last_seen=utcnow()))
        s.flush()
        s.add(RemoteCommand(bridge_id="nuevo", printer_id="p", action="pause", status="done",
                            updated_at=utcnow() - timedelta(days=2)))
    services.forget_old_data()
    with f.db.session() as s:
        assert s.get(Job, job["id"]) is None
        assert [b.id for b in s.query(Bridge).all()] == ["nuevo"]
        assert s.query(RemoteCommand).count() == 0
    # The hours used stay in the month's usage.
    assert client.get("/v1/me", headers=ana).status_code == 200


def test_no_request_log_with_addresses_by_default():
    from tisma_cloud.settings import Settings

    assert Settings().access_log is False


def test_pages_run_only_their_own_scripts(client):
    import base64
    import hashlib

    r = client.get("/cuenta")
    csp = r.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]


def test_registering_a_known_address_many_times_answers_the_same(client, f):
    with_password(f, "ana@x.com")
    sent = client.app.state.services.mailer.sent
    answers = [client.post("/v1/auth/register", json={"email": "ana@x.com", "password": "otra clave larga"})
               for _ in range(3)]
    # The owner gets one notice a minute, and the answer never changes (a 429 would tell the address is known).
    assert [r.status_code for r in answers] == [201, 201, 201]
    assert len([m for m in sent if m[0] == "ana@x.com"]) == 1
