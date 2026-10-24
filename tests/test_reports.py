"""Problem reports from Tisma and the app: nothing personal is kept, the same problem is grouped, the catalog for the
programmers, and the reports go with the account (copy, deletion, what is forgotten)."""

from __future__ import annotations

import json
from datetime import timedelta

from conftest import new_app
from fastapi.testclient import TestClient
from sqlalchemy import text

from tisma_cloud import reports
from tisma_cloud.models import BugReport, utcnow

CRASH_LOG = """[2026-10-23 18:24:51.204446] [0x00007f64ccff96c0] [info] Loading C:\\Users\\Ana Perez\\AppData\\Roaming\\TismaSlicer\\PrusaSlicer.ini
[2026-10-23 18:24:52.100000] [0x00007f64ccff96c0] [info] Bambu printer 192.168.1.40 access code: 12345678 serial: 01P00A123456789
[2026-10-23 18:24:53.000000] [0x00007f64ccff96c0] [error] Exception in slicing: std::out_of_range at /home/ana/models/part.3mf layer 12
[2026-10-23 18:24:53.100000] [0x00007f64ccff96c0] [info] user ana.perez@example.com token tu_abcdefghijklmnopqrstuv
"""


def report(**over) -> dict:
    body = {"app": "pc", "kind": "crash", "title": "Crash when slicing", "description": "It closed while slicing",
            "steps": "Open part.3mf and slice", "app_version": "2.9.6-tisma", "os": "Windows 11", "log": CRASH_LOG}
    body.update(over)
    return body


def test_nothing_personal_is_kept():
    text = reports.redact(CRASH_LOG + "Authorization: Bearer abc.def.ghi\npassword=hunter2 apikey: XYZ fe80::1:2:3:4\n"
                          "/Users/ana/Library 0123456789abcdef0123456789abcdef")
    for secret in ("Ana Perez", "12345678", "01P00A123456789", "192.168.1.40", "/home/ana", "ana.perez@example.com",
                   "tu_abcdefghijklmnopqrstuv", "abc.def.ghi", "hunter2", "XYZ", "fe80::1:2:3:4", "/Users/ana/",
                   "0123456789abcdef0123456789abcdef"):
        assert secret not in text, secret
    # What is needed to find the problem stays.
    assert "std::out_of_range" in text and "layer 12" in text and "TismaSlicer" in text


def test_the_same_problem_has_the_same_signature():
    other_run = CRASH_LOG.replace("18:24:53", "09:01:02").replace("layer 12", "layer 40").replace("/home/ana", "/home/bob")
    assert reports.signature("pc", "a", CRASH_LOG) == reports.signature("pc", "b", other_run)
    # Another error, another app: another problem.
    assert reports.signature("pc", "a", CRASH_LOG) != reports.signature("pc", "a", "[error] Out of memory")
    assert reports.signature("pc", "a", CRASH_LOG) != reports.signature("android", "a", CRASH_LOG)
    # Without a log, the words of the title (in any order, any case).
    assert reports.signature("pc", "Supports missing on the plate", "") == \
        reports.signature("pc", "the plate: SUPPORTS missing", "")


def test_a_report_without_account_is_kept_clean(client):
    r = client.post("/v1/reports", json=report(title="Crash at ana@x.com"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["similar"] == 0 and len(out["signature"]) == 16
    with client.app.state.services.db.session() as s:
        row = s.get(BugReport, out["id"])
        assert row.user_id is None and row.title == "Crash at <email>" and row.status == "new"
        assert "12345678" not in row.log and "std::out_of_range" in row.log
        # Encrypted in the database.
        raw = s.execute(text("SELECT log, description FROM bug_reports WHERE id = :id"), {"id": out["id"]}).one()
        assert raw.log.startswith("enc1:") and raw.description.startswith("enc1:")
    # A second report of the same problem knows it is not alone.
    assert client.post("/v1/reports", json=report()).json()["similar"] == 1


def test_wrong_values_and_tokens_are_refused(client):
    assert client.post("/v1/reports", json=report(app="ios")).status_code == 422
    assert client.post("/v1/reports", json=report(kind="spam")).status_code == 422
    assert client.post("/v1/reports", json=report(title="x")).status_code == 422
    assert client.post("/v1/reports", json=report(), headers={"Authorization": "Bearer tu_wrong"}).status_code == 401


def test_reports_are_limited(settings):
    settings.anonymous_reports_per_hour = 2
    settings.reports_per_hour = 3
    with TestClient(new_app(settings), client=("200.40.10.8", 1)) as c:
        assert [c.post("/v1/reports", json=report()).status_code for _ in range(3)] == [201, 201, 429]
        # A body larger than the limit is refused before it is read.
        big = json.dumps(report(log="x" * 600_000))
        r = c.post("/v1/reports", content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413


def test_the_account_sees_its_reports(client, f):
    ana = f.user("ana@x.com")
    client.post("/v1/reports", json=report(title="Supports missing"), headers=ana)
    client.post("/v1/reports", json=report(title="Other people's report"))
    mine = client.get("/v1/me/reports", headers=ana).json()["reports"]
    assert [r["title"] for r in mine] == ["Supports missing"] and mine[0]["status"] == "new"
    assert "log" not in mine[0]


def test_catalog_for_the_programmers(client, f):
    admin = f.user("admin@x.com", admin=True)
    for _ in range(3):
        client.post("/v1/reports", json=report())
    client.post("/v1/reports", json=report(app="android", kind="bug", title="The camera does not open", log=""))
    groups = client.get("/v1/admin/reports", headers=admin).json()["groups"]
    assert [g["reports"] for g in groups] == [3, 1]
    assert groups[0]["first_error"].endswith("layer 12") and "Windows 11" in groups[0]["systems"]

    md = client.get("/v1/admin/reports/catalog", headers=admin)
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "## 1. Crash when slicing (pc, 3 reports)" in md.text and "std::out_of_range" in md.text
    assert "12345678" not in md.text and "Ana Perez" not in md.text
    lines = client.get("/v1/admin/reports/catalog?format=jsonl", headers=admin).text.splitlines()
    assert [json.loads(x)["app"] for x in lines] == ["pc", "android"]

    # Fixed: out of the open catalog, still in the list of the fixed ones.
    sig = groups[0]["signature"]
    r = client.patch(f"/v1/admin/report-groups/{sig}", json={"status": "fixed", "note": "fixed in 47e2c11"}, headers=admin)
    assert r.json() == {"updated": 3}
    assert [g["app"] for g in client.get("/v1/admin/reports", headers=admin).json()["groups"]] == ["android"]
    fixed = client.get("/v1/admin/reports?status=fixed", headers=admin).json()["groups"]
    assert fixed[0]["reports"] == 3
    one = fixed[0]["report_ids"][0]
    assert client.get(f"/v1/admin/reports/{one}", headers=admin).json()["note"] == "fixed in 47e2c11"
    assert client.patch(f"/v1/admin/reports/{one}", json={"status": "nope"}, headers=admin).status_code == 422
    assert client.get("/v1/admin/reports?status=nope", headers=admin).status_code == 422
    # Only for administrators.
    assert client.get("/v1/admin/reports", headers=f.user("bob@x.com")).status_code == 403


def test_reports_go_with_the_account(client, f):
    services = client.app.state.services
    ana = f.user("ana@x.com")
    client.post("/v1/reports", json=report(), headers=ana)
    exported = client.get("/v1/me/export", headers=ana).json()["problem_reports"]
    assert exported[0]["title"] == "Crash when slicing" and "std::out_of_range" in exported[0]["log"]
    with services.db.session() as s:
        user_id = s.get(BugReport, exported[0]["id"]).user_id
    services.delete_account(user_id)
    with services.db.session() as s:
        assert s.query(BugReport).count() == 0


def test_old_reports_are_forgotten(client):
    services = client.app.state.services
    client.post("/v1/reports", json=report())
    with services.db.session() as s:
        s.query(BugReport).update({"created_at": utcnow() - timedelta(days=services.settings.report_days + 1)})
    client.post("/v1/reports", json=report(title="A new one"))
    services.forget_old_data()
    with services.db.session() as s:
        assert [r.title for r in s.query(BugReport)] == ["A new one"]


def test_catalog_from_the_console(client, settings, monkeypatch, capsys, tmp_path):
    from tisma_cloud import admin as admin_cli

    client.post("/v1/reports", json=report())
    client.post("/v1/reports", json=report(app="android", title="The camera does not open", log=""))
    monkeypatch.setenv("TISMA_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("TISMA_STORAGE_DIR", str(settings.storage_dir))
    out = tmp_path / "problems.md"
    assert admin_cli.main(["reports", "--out", str(out)]) == 0
    assert "2 problems" in capsys.readouterr().out
    assert "## 1. " in out.read_text(encoding="utf-8") and "std::out_of_range" in out.read_text(encoding="utf-8")
    assert admin_cli.main(["reports", "--format", "jsonl", "--app", "android"]) == 0
    assert [json.loads(x)["title"] for x in capsys.readouterr().out.splitlines()] == ["The camera does not open"]
    assert admin_cli.main(["reports", "--status", "nope"]) == 2


def test_a_useful_report_is_rewarded_by_hand(client, f, settings):
    services = client.app.state.services
    admin = f.user("admin@x.com", admin=True)
    ana = f.user("ana@x.com", paid_days=None)
    client.post("/v1/reports", json=report(title="Without account"))
    first = client.post("/v1/reports", json=report(), headers=ana).json()
    client.post("/v1/reports", json=report(), headers=f.user("bob@x.com", paid_days=10))
    sig = first["signature"]

    # The first one who told it with an account: Ana (the anonymous report is older but has nobody to reward).
    r = client.post(f"/v1/admin/report-groups/{sig}/reward", json={}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["report"] == first["id"] and r.json()["days"] == settings.report_reward_days
    me = f.me(ana)
    assert me["paid"] and me["paid_until"] is not None
    mine = client.get("/v1/me/reports", headers=ana).json()["reports"][0]
    assert mine["reward_days"] == settings.report_reward_days and mine["status"] == "confirmed"
    sent = services.mailer.sent[-1]
    assert sent[0] == "ana@x.com" and "7 días" in sent[2]

    # Once per report; the next reward of the group goes to the next one (Bob, whose paid days are extended).
    assert client.post(f"/v1/admin/reports/{first['id']}/reward", json={}, headers=admin).status_code == 409
    with services.db.session() as s:
        from tisma_cloud.models import User
        from tisma_cloud import crypto
        bob_until = s.query(User).filter_by(email_index=crypto.index("bob@x.com")).one().paid_until
    r = client.post(f"/v1/admin/report-groups/{sig}/reward", json={"days": 3}, headers=admin)
    assert r.json()["days"] == 3
    with services.db.session() as s:
        assert s.query(User).filter_by(email_index=crypto.index("bob@x.com")).one().paid_until == bob_until + timedelta(days=3)
    # Nobody left with an account; an anonymous report has nobody to reward.
    assert client.post(f"/v1/admin/report-groups/{sig}/reward", json={}, headers=admin).status_code == 404
    with services.db.session() as s:
        anonymous = s.query(BugReport).filter(BugReport.user_id.is_(None)).one().id
    assert client.post(f"/v1/admin/reports/{anonymous}/reward", json={}, headers=admin).status_code == 409
    groups = client.get("/v1/admin/reports", headers=admin).json()["groups"]
    assert (groups[0]["with_account"], groups[0]["rewarded"]) == (2, 2)
    # Only administrators reward.
    assert client.post(f"/v1/admin/report-groups/{sig}/reward", json={}, headers=ana).status_code == 403


def test_rewards_are_limited_per_year(client, f, settings):
    settings.report_rewards_per_year = 2
    admin = f.user("admin@x.com", admin=True)
    ana = f.user("ana@x.com")
    ids = [client.post("/v1/reports", json=report(title=f"Problem number {w}", log=""), headers=ana).json()["id"]
           for w in ("one", "two", "three")]
    codes = [client.post(f"/v1/admin/reports/{i}/reward", json={}, headers=admin).status_code for i in ids]
    assert codes == [200, 200, 409]
    assert client.post(f"/v1/admin/reports/{ids[2]}/reward", json={"days": 500}, headers=admin).status_code == 422


def test_kind_of_error_key_words_and_summary():
    from tisma_cloud import triage

    assert triage.category("bug", "Faltan los soportes bajo el voladizo",
                           "Laminé una pieza con voladizo y no aparecen soportes") == "supports"
    assert triage.category("bug", "The camera does not open", "It stays black") == "camera"
    assert triage.category("bug", "A impressora não conecta pelo wifi", "") == "printer"
    assert triage.category("crash", "Se cierra", "") == "crash"
    assert triage.category("bug", "Planchado deja marcas", "") == "slicing"   # "plan" is not "planchado"
    assert triage.category("bug", "Algo raro", "") == "other"
    corpus = triage.Corpus(["soportes voladizo pieza", "camera black", "cierra laminar", "camera wifi"])
    assert triage.keywords("Faltan los soportes bajo el voladizo y no aparecen soportes", corpus)[:2] == \
        ["soportes", "voladizo"]
    text = triage.summary("supports", "Faltan los soportes", ["Laminé una pieza con voladizo de 60 grados y no hay "
                          "soportes. Gracias. Uso la última versión."], corpus, "std::out_of_range at layer 12",
                          triage.printer_of("Tisma\nPrinter: Kobra 3 0.4 nozzle (Anycubic Kobra 3), print: x\n"))
    assert text.startswith("Soportes: Faltan los soportes. Laminé una pieza con voladizo de 60 grados")
    assert "Uso la última" not in text and "Error: std::out_of_range" in text and "Impresora: Anycubic Kobra 3." in text


def test_filters_list_and_category_by_hand(client, f):
    admin = f.user("admin@x.com", admin=True)
    client.post("/v1/reports", json=report(kind="bug", title="Faltan los soportes bajo el voladizo",
                                           description="No aparecen los soportes de árbol", log=""))
    client.post("/v1/reports", json=report(app="android", kind="bug", title="The camera does not open",
                                           description="It stays black", log="", app_version="1.4.0"))
    client.post("/v1/reports", json=report())   # a crash while slicing
    page = client.get("/v1/admin/reports", headers=admin).json()
    assert page["reports"] == 3 and page["apps"] == ["android", "pc"] and "1.4.0" in page["versions"]
    assert {c["key"]: c["reports"] for c in page["categories"]}["supports"] == 1
    by_kind = {g["category"]: g for g in page["groups"]}
    assert set(by_kind) == {"supports", "camera", "crash"}
    assert by_kind["crash"]["summary"].startswith("Cierre o cuelgue: Crash when slicing.")
    assert "std::out_of_range" in by_kind["crash"]["summary"]

    def titles(**params):
        return [r["title"] for r in client.get("/v1/admin/reports/list", params=params, headers=admin).json()["reports"]]

    assert titles(category="camera") == ["The camera does not open"]
    assert titles(app="android") == ["The camera does not open"]
    assert titles(version="1.4.0") == ["The camera does not open"]
    assert titles(q="ARBOL soportes") == ["Faltan los soportes bajo el voladizo"]    # any case and accents
    assert titles(q="out_of_range") == ["Crash when slicing"]                      # the error of the log
    assert titles(q="nothing like this") == []
    one = client.get("/v1/admin/reports/list", params={"category": "camera"}, headers=admin).json()["reports"][0]
    assert one["keywords"] and one["summary"].startswith("Cámara: The camera does not open.")

    # The team chooses another kind: it stays.
    r = client.patch(f"/v1/admin/reports/{one['id']}", json={"category": "interface"}, headers=admin)
    assert r.json()["category"] == "interface"
    assert titles(category="interface") == ["The camera does not open"]
    assert client.patch(f"/v1/admin/reports/{one['id']}", json={"category": "nope"}, headers=admin).status_code == 422


def test_reports_are_translated_on_our_servers(client, f):
    """With a translator (a LibreTranslate that answers as the real one): the texts in another language are kept
    translated, searched and classified on the translation."""
    from tisma_cloud import translate

    services = client.app.state.services
    admin = f.user("admin@x.com", admin=True)
    words = {"A impressora não conecta": "La impresora no conecta", "depois da atualização": "después de la actualización",
             "Faltam os suportes": "Faltan los soportes"}

    class FakeTranslator(translate.Translator):
        def translate(self, text):
            if text in words:
                return words[text], "pt"
            if text == "down":
                raise translate.TranslationError("connection refused")
            return text, "es"

    client.post("/v1/reports", json=report(kind="bug", title="A impressora não conecta", description="depois da atualização", log=""))
    client.post("/v1/reports", json=report(kind="bug", title="Faltam os suportes", description="", log=""))
    client.post("/v1/reports", json=report(kind="bug", title="Se cierra al abrir", description="Ya está en castellano", log=""))
    assert services.translate_reports() == 0          # no translator: kept as written
    services.translator = FakeTranslator("http://translator", "es")
    assert services.translate_reports() == 3
    assert services.translate_reports() == 0          # nothing left
    rows = {r["title"]: r for r in client.get("/v1/admin/reports/list", headers=admin).json()["reports"]}
    printer = rows["A impressora não conecta"]
    assert printer["language"] == "pt" and printer["translation"] == {
        "title": "La impresora no conecta", "description": "después de la actualización"}
    # The title as written goes with its translation.
    assert printer["summary"].startswith("Impresora y conexión: La impresora no conecta (A impressora não conecta).")
    assert rows["Faltam os suportes"]["category"] == "supports"
    assert rows["Se cierra al abrir"]["language"] == "es" and rows["Se cierra al abrir"]["translation"] == {}
    # Searched in the language of the team too.
    found = client.get("/v1/admin/reports/list", params={"q": "actualizacion"}, headers=admin).json()["reports"]
    assert [r["title"] for r in found] == ["A impressora não conecta"]
    md = client.get("/v1/admin/reports/catalog", headers=admin).text
    assert "Report 1, translated" in md and "después de la actualización" in md

    # The translator down: the report waits for it.
    client.post("/v1/reports", json=report(kind="bug", title="down", description="", log=""))
    assert services.translate_reports() == 0
    with services.db.session() as s:
        assert s.query(BugReport).filter(BugReport.translated_at.is_(None)).count() == 1


def test_translator_speaks_libretranslate():
    """The request and the answer of LibreTranslate (docs.libretranslate.com, API usage), with a local server."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from tisma_cloud.translate import TranslationError, Translator

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body))
            answer = json.dumps({"detectedLanguage": {"confidence": 100.0, "language": "en"},
                                 "translatedText": "Faltan los soportes"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):  # noqa: ANN002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        out = Translator(f"http://127.0.0.1:{server.server_port}/", "es").translate("Supports are missing")
        assert out == ("Faltan los soportes", "en")
        assert seen == [("/translate", {"q": "Supports are missing", "source": "auto", "target": "es", "format": "text"})]
    finally:
        server.shutdown()
    try:
        Translator(f"http://127.0.0.1:{server.server_port}", "es", timeout=2).translate("x")
        raise AssertionError("no error")
    except TranslationError:
        pass


def test_search_by_fields_and_specific_filters(client, f):
    admin = f.user("admin@x.com", admin=True)
    ana = f.user("ana@x.com")
    kobra = "Tisma\nWindows 11\nPrinter: Kobra 3 0.4 nozzle (Anycubic Kobra 3), print: x\n--- log ---\n[e] [1] [error]   std::out_of_range\n"
    bambu = "Tisma\nLinux\nPrinter: Bambu Lab A1 0.4 nozzle (Bambu Lab A1), print: y\n--- log ---\n[e] [1] [error]   Access denied\n"
    client.post("/v1/reports", json=report(title="Crash on the Kobra", log=kobra, os="Windows 11 Pro", app_version="0.9.0"))
    client.post("/v1/reports", json=report(title="Crash on the A1", log=bambu, os="Ubuntu Linux 24.04", app_version="0.9.1"),
                headers=ana)
    page = client.get("/v1/admin/reports", headers=admin).json()
    assert page["printers"] == ["Anycubic Kobra 3", "Bambu Lab A1"]
    assert page["systems"] == ["Linux", "Windows"] and page["versions"] == ["0.9.0", "0.9.1"]

    def titles(**params):
        return [r["title"] for r in client.get("/v1/admin/reports/list", params=params, headers=admin).json()["reports"]]

    assert titles(q="impresora:kobra") == ["Crash on the Kobra"]
    assert titles(q='printer:"bambu lab"') == ["Crash on the A1"]
    assert titles(q="version:0.9.1 crash") == ["Crash on the A1"]
    assert titles(q="error:out_of_range") == ["Crash on the Kobra"]
    assert titles(q="sistema:linux") == ["Crash on the A1"]
    assert titles(q="tipo:cierre") == ["Crash on the A1", "Crash on the Kobra"]
    assert titles(q="impresora:kobra sistema:linux") == []
    assert titles(q='impresora:"kobra') == ["Crash on the Kobra"]       # an open quote
    assert titles(printer="Bambu Lab A1") == ["Crash on the A1"]
    assert titles(system="Windows") == ["Crash on the Kobra"]
    assert titles(account="1") == ["Crash on the A1"]
    today = utcnow().date().isoformat()
    assert len(titles(since=today, until=today)) == 2
    assert titles(until="2020-01-01") == []
