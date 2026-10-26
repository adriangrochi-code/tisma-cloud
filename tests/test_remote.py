"""Remote printers: the bridge at home relays its printers, the phone sees and commands them (remote.py)."""

from __future__ import annotations

import time
from datetime import timedelta

from tisma_cloud import remote
from tisma_cloud.models import RemoteCommand, utcnow

BRIDGE = "pc-casa-1"
PRINTERS = [
    {"id": "a1", "name": "A1 mini", "kind": "bambu", "camera": True, "actions": ["print", "pause", "resume", "stop"],
     "state": {"state": "idle", "nozzle_temp": 25.5, "bed_temp": 24, "percent": -1, "job_name": "x" * 500,
               "secret": "no", "layer": True}},
    {"id": "ender", "name": "Ender 3", "kind": "octoprint", "camera": False, "actions": ["print", "bogus"]},
    {"id": "bad id/../", "name": "nope"},
]


def register(client, headers, bridge=BRIDGE, printers=PRINTERS):
    r = client.put(f"/v1/bridges/{bridge}", headers=headers, json={"name": "PC del taller", "printers": printers})
    assert r.status_code == 200, r.text
    return r.json()


def test_bridge_state_is_cleaned_and_listed(client, f):
    ana = f.user("ana@x.com")
    # Nobody looks: the state every 30 s.
    assert register(client, ana) == {"watch": [], "state_s": 30}
    bridges = client.get("/v1/remote/printers", headers=ana).json()["bridges"]
    # The phone looks: every 3 s.
    assert register(client, ana) == {"watch": [], "state_s": 3}
    assert len(bridges) == 1 and bridges[0]["online"] and bridges[0]["name"] == "PC del taller"
    printers = {p["id"]: p for p in bridges[0]["printers"]}
    # Unknown fields, wrong kinds, bad ids and unknown actions are dropped; long texts are cut.
    assert set(printers) == {"a1", "ender"} and not printers["a1"]["locked"] and not printers["ender"]["locked"]
    assert printers["a1"]["state"] == {"state": "idle", "nozzle_temp": 25.5, "bed_temp": 24, "percent": -1,
                                       "job_name": "x" * 200}
    assert printers["ender"]["actions"] == ["print"] and printers["ender"]["state"] == {}
    # Another account sees nothing and cannot take the bridge.
    bruno = f.user("bruno@x.com")
    assert client.get("/v1/remote/printers", headers=bruno).json()["bridges"] == []
    assert client.put(f"/v1/bridges/{BRIDGE}", headers=bruno, json={"printers": []}).status_code == 404
    assert client.put("/v1/bridges/bad.id", headers=ana, json={"printers": []}).status_code == 404


def test_print_from_the_phone_through_the_bridge(client, f):
    ana = f.user("ana@x.com")
    register(client, ana)
    gcode = b"G28\nG1 X10\n"
    r = client.post(f"/v1/remote/bridges/{BRIDGE}/printers/a1/print", headers=ana,
                    files={"file": ("pieza final.gcode", gcode)})
    assert r.status_code == 201, r.text
    command = r.json()
    assert command["status"] == "queued" and command["file_name"] == "pieza final.gcode"
    # The bridge takes it (once), downloads the file and tells it printed.
    taken = client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana).json()["commands"]
    assert [c["id"] for c in taken] == [command["id"]] and taken[0]["status"] == "sent"
    assert client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana).json()["commands"] == []
    file = client.get(f"/v1/bridges/{BRIDGE}/commands/{command['id']}/file", headers=ana)
    assert file.status_code == 200 and file.content == gcode
    done = client.post(f"/v1/bridges/{BRIDGE}/commands/{command['id']}", headers=ana, json={"ok": True})
    assert done.json()["status"] == "done"
    assert client.get(f"/v1/remote/commands/{command['id']}", headers=ana).json()["status"] == "done"
    # The file is not kept once printed; the command cannot be answered twice.
    assert not client.app.state.services.remote_files.print_file(command["id"]).exists()
    assert client.post(f"/v1/bridges/{BRIDGE}/commands/{command['id']}", headers=ana, json={"ok": False}).status_code == 404
    # Another account does not see the command.
    assert client.get(f"/v1/remote/commands/{command['id']}", headers=f.user("bruno@x.com")).status_code == 404


def test_commands_checked_before_they_reach_the_bridge(client, f):
    ana = f.user("ana@x.com")
    register(client, ana)
    base = f"/v1/remote/bridges/{BRIDGE}/printers"
    # Only G-code; only the actions the printer can do; only known printers and actions.
    assert client.post(f"{base}/a1/print", headers=ana, files={"file": ("pieza.stl", b"solid")}).status_code == 422
    assert client.post(f"{base}/ender/pause", headers=ana).status_code == 422
    assert client.post(f"{base}/nope/pause", headers=ana).status_code == 404
    assert client.post(f"{base}/a1/explode", headers=ana).status_code == 404
    r = client.post(f"{base}/a1/pause", headers=ana)
    assert r.status_code == 201 and r.json()["action"] == "pause"
    failed = client.post(f"/v1/bridges/{BRIDGE}/commands/{r.json()['id']}", headers=ana, json={"ok": False})
    assert failed.status_code == 404   # not taken by the bridge yet
    taken = client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana).json()["commands"]
    failed = client.post(f"/v1/bridges/{BRIDGE}/commands/{taken[0]['id']}", headers=ana,
                         json={"ok": False, "error": "The printer is not printing"})
    assert failed.json()["status"] == "failed" and failed.json()["error"] == "The printer is not printing"


def test_offline_bridge_and_late_commands(client, f):
    ana = f.user("ana@x.com")
    register(client, ana)
    services = client.app.state.services
    command = client.post(f"/v1/remote/bridges/{BRIDGE}/printers/a1/print", headers=ana,
                          files={"file": ("p.gcode", b"G28\n")}).json()
    # Not taken in time: it fails and its file goes, so a print never starts long after it was asked for.
    with services.db.session() as s:
        s.get(RemoteCommand, command["id"]).updated_at = utcnow() - timedelta(minutes=11)
    services.sweep()
    late = client.get(f"/v1/remote/commands/{command['id']}", headers=ana).json()
    assert late["status"] == "failed" and "did not take" in late["error"]
    assert not services.remote_files.print_file(command["id"]).exists()
    assert client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana).json()["commands"] == []
    # A bridge that stopped sending is offline: no new commands.
    services.presence.seen(BRIDGE, utcnow() - timedelta(minutes=5))
    services.flush_presence()
    assert client.get("/v1/remote/printers", headers=ana).json()["bridges"][0]["online"] is False
    assert client.post(f"/v1/remote/bridges/{BRIDGE}/printers/a1/pause", headers=ana).status_code == 409


def test_camera_only_while_someone_looks(client, f):
    ana = f.user("ana@x.com")
    register(client, ana)
    camera = f"/v1/remote/bridges/{BRIDGE}/printers/a1/camera"
    # Nobody looked: the bridge sends nothing. The phone asks: no image yet, and the bridge is told to send.
    assert client.get(camera, headers=ana).status_code == 204
    assert register(client, ana) == {"watch": ["a1"], "state_s": 3}
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    upload = f"/v1/bridges/{BRIDGE}/printers/a1/camera"
    assert client.post(upload, headers=ana, content=b"GIF89a").status_code == 422
    assert client.post(upload, headers=ana, content=b"\xff\xd8" + b"0" * remote.MAX_IMAGE_BYTES).status_code == 413
    assert client.post(upload, headers=ana, content=jpeg).status_code == 200
    image = client.get(camera, headers=ana)
    assert image.status_code == 200 and image.content == jpeg and image.headers["content-type"] == "image/jpeg"
    # The images of the homes are kept in memory only, never on disk.
    storage = client.app.state.services.settings.storage_dir
    assert not [p for p in storage.rglob("*") if p.is_file() and p.read_bytes() == jpeg]
    # A printer without a camera the bridge can relay.
    assert client.get(f"/v1/remote/bridges/{BRIDGE}/printers/ender/camera", headers=ana).status_code == 409


def test_remove_a_bridge(client, f):
    ana = f.user("ana@x.com")
    register(client, ana)
    command = client.post(f"/v1/remote/bridges/{BRIDGE}/printers/a1/print", headers=ana,
                          files={"file": ("p.gcode", b"G28\n")}).json()
    assert client.delete(f"/v1/remote/bridges/{BRIDGE}", headers=ana).json() == {"ok": True}
    assert client.get("/v1/remote/printers", headers=ana).json()["bridges"] == []
    assert not client.app.state.services.remote_files.print_file(command["id"]).exists()
    assert client.get(f"/v1/remote/commands/{command['id']}", headers=ana).status_code == 404


def test_long_poll_wakes_with_a_command(client, f):
    import threading
    import time

    ana = f.user("ana@x.com")
    register(client, ana)
    sent: list = []

    def later() -> None:
        time.sleep(1.5)
        sent.append(client.post(f"/v1/remote/bridges/{BRIDGE}/printers/a1/stop", headers=ana).json())

    threading.Thread(target=later).start()
    started = time.monotonic()
    commands = client.get(f"/v1/bridges/{BRIDGE}/commands?wait=10", headers=ana).json()["commands"]
    assert 1. < time.monotonic() - started < 6.
    assert [c["action"] for c in commands] == ["stop"]


def test_bridge_state_in_memory_written_in_batches(client, f):
    from tisma_cloud.models import Bridge

    ana = f.user("ana@x.com")
    services = client.app.state.services
    register(client, ana)
    # Only readings changed: the phone sees them at once, the database gets them later.
    busy = [dict(PRINTERS[0], state={"state": "printing", "percent": 42})] + PRINTERS[1:]
    register(client, ana, printers=busy)
    printers = client.get("/v1/remote/printers", headers=ana).json()["bridges"][0]["printers"]
    assert printers[0]["state"] == {"state": "printing", "percent": 42}
    with services.db.session() as s:
        assert '"percent": -1' in s.get(Bridge, BRIDGE).printers
    services.flush_presence()
    with services.db.session() as s:
        assert '"percent": 42' in s.get(Bridge, BRIDGE).printers
    # A printer added: written at once.
    register(client, ana, printers=busy[:1])
    with services.db.session() as s:
        assert [p["id"] for p in s.get(Bridge, BRIDGE).printer_list] == ["a1"]


def test_the_phone_looking_wakes_the_bridge_for_its_state(client, f):
    import threading
    import time

    ana = f.user("ana@x.com")
    register(client, ana)
    answers: list = []

    def wait() -> None:
        answers.append((client.get(f"/v1/bridges/{BRIDGE}/commands?wait=10", headers=ana).json(), time.monotonic()))

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.5)
    looked = time.monotonic()
    client.get("/v1/remote/printers", headers=ana)
    waiter.join(10)
    # The wait ends at once without commands, and the bridge sends its state every 3 s while the phone looks.
    assert answers and answers[0][0] == {"commands": [], "state_s": 3} and answers[0][1] - looked < 3.


def test_a_waiting_bridge_costs_no_query(client, f):
    from sqlalchemy import event

    ana = f.user("ana@x.com")
    register(client, ana)
    client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana)
    services = client.app.state.services
    engine = services.db.engine
    queries: list[str] = []

    def count(conn, cursor, statement, *args) -> None:  # noqa: ANN001
        queries.append(statement)

    # The sweep at start-up runs in the background and checks the alerts last: wait until it gets there, and hold the
    # alerts back while counting, so that its queries are not counted as the bridge's (slower databases overlap).
    deadline = time.monotonic() + 30
    while services._alerts_checked is None and time.monotonic() < deadline:
        time.sleep(0.05)
    with services._alerts_lock:
        event.listen(engine, "before_cursor_execute", count)
        try:
            for _ in range(5):
                assert client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana).json()["commands"] == []
        finally:
            event.remove(engine, "before_cursor_execute", count)
    assert queries == []


def test_a_bridge_removed_while_its_state_is_written(client, f):
    ana = f.user("ana@x.com")
    services = client.app.state.services
    register(client, ana)
    register(client, ana, bridge="pc-otra")
    client.get(f"/v1/bridges/{BRIDGE}/commands?wait=0", headers=ana)
    client.get("/v1/bridges/pc-otra/commands?wait=0", headers=ana)
    # Removed from the database by another process: the other bridges are still written.
    from tisma_cloud.models import Bridge

    with services.db.session() as s:
        s.delete(s.get(Bridge, "pc-otra"))
    services.presence.seen(BRIDGE, utcnow() + timedelta(seconds=30))
    services.flush_presence()
    with services.db.session() as s:
        assert s.get(Bridge, BRIDGE).last_seen > utcnow() + timedelta(seconds=20)
