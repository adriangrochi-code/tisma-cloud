"""Why a node does not reach its master, said for a person (node/connection.py), in its diagnosis and its status."""

from __future__ import annotations

import errno
import socket
import ssl

import httpx
import pytest

from tisma_cloud.node.connection import STATE_FILE, StateFile, explain, explain_status, read_state, version_note

URL = "http://192.168.1.10:8000"


def wrapped(cause: BaseException) -> httpx.ConnectError:
    """As httpx raises them: a ConnectError over the socket's own error."""
    try:
        try:
            raise cause
        except BaseException as inner:
            raise httpx.ConnectError(str(inner)) from inner
    except httpx.ConnectError as ex:
        return ex


@pytest.mark.parametrize("ex, words", [
    (wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "refused")), "Nada atiende en 192.168.1.10:8000"),
    (httpx.ConnectTimeout("timed out"), "su puerto está bloqueado (en el maestro: «tisma-cloud firewall on»)"),
    (wrapped(TimeoutError("timed out")), "su puerto está bloqueado"),
    (wrapped(socket.gaierror(socket.EAI_NONAME, "Name or service not known")),
     "192.168.1.10 no existe en el DNS: revisá el dominio"),
    (wrapped(socket.gaierror(11001, "No such host is known")), "192.168.1.10 no existe en el DNS"),
    (wrapped(socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")),
     "No se pudo consultar el DNS por 192.168.1.10: esta máquina no tiene red todavía"),
    (wrapped(OSError(errno.ENETUNREACH, "Network is unreachable")), "no tiene red, o la dirección es de otra red"),
    (wrapped(ssl.SSLCertVerificationError("certificate verify failed")), "El certificado HTTPS"),
    (httpx.ReadTimeout("read"), "tardó demasiado en contestar"),
])
def test_each_failure_is_said_for_a_person(ex, words):
    said = explain(ex, URL)
    assert words in said and "Error" not in said.split(":")[0]


def test_windows_error_numbers_of_an_unreachable_network():
    ex = OSError(10065, "A socket operation was attempted to an unreachable host")
    assert "no tiene red" in explain(wrapped(ex), URL)


def test_answers_of_the_master():
    assert "no reconoce el token" in explain_status(401, URL)
    assert "deshabilitado" in explain_status(403, URL)
    assert "otro programa" in explain_status(404, URL)
    response = httpx.Response(401, request=httpx.Request("POST", URL + "/v1/node/claim"))
    assert "no reconoce el token" in explain(httpx.HTTPStatusError("401", request=response.request, response=response),
                                             URL)


def test_versions():
    assert version_note("0.2.0", "0.2.0") is None
    assert "versión 0.3.0 y este nodo la 0.2.0" in version_note("0.3.0", "0.2.0")


def test_the_state_file_is_written_only_when_it_changes(tmp_path):
    path = tmp_path / STATE_FILE
    state = StateFile(path)
    state.write(True, "Conectado")
    first = path.stat().st_mtime_ns
    state.write(True, "Conectado")
    assert path.stat().st_mtime_ns == first
    state.write(False, "Nada atiende")
    assert read_state(path)["message"] == "Nada atiende" and read_state(path)["ok"] is False
    assert read_state(tmp_path / "none.json") is None


class _Keep:
    """The test client given to the diagnosis as its httpx.Client, not closed by its «with»."""

    def __init__(self, client):
        self.client = client

    def __call__(self, **_kw):
        return self

    def __enter__(self):
        return self.client

    def __exit__(self, *_a):
        return False


def test_the_diagnosis_of_a_node_says_what_the_master_thinks_of_it(client, f, tmp_path, monkeypatch):
    from tisma_cloud.manager import Manager

    admin = f.user("root@x.com", admin=True)
    token = f.node("n", "slice")["Authorization"].split(" ", 1)[1]
    m = Manager(tmp_path)
    m.set_role("node")
    m.write_node({"master_url": "http://testserver", "token": token})
    monkeypatch.setattr(httpx, "Client", _Keep(client))

    def master_checks():
        return {c.name: (c.ok, c.detail) for c in m._master_checks(m.read_node())}

    checks = master_checks()
    assert checks["Maestro"][0] is True and checks["Versión"][0] is True
    assert checks["Token del nodo"] == (True, "El maestro lo conoce: nodo «n»")
    # The window's check does not count as the node being seen.
    assert client.get("/v1/admin/nodes", headers=admin).json()["nodes"][0]["last_seen"] is None
    client.patch("/v1/admin/nodes/n", headers=admin, json={"enabled": False})
    assert "deshabilitado" in master_checks()["Token del nodo"][1]
    m.write_node({"token": "tn_unknown"})
    assert "no reconoce el token" in master_checks()["Token del nodo"][1]


def test_the_status_of_a_running_node_says_what_it_saw(tmp_path, monkeypatch):
    from tisma_cloud import manager as manager_module
    from tisma_cloud.manager import Manager

    m = Manager(tmp_path)
    m.set_role("node")
    monkeypatch.setattr(manager_module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(manager_module, "_process_start", lambda pid: None)
    m.pid_path.write_text("4242\n")
    StateFile(tmp_path / STATE_FILE).write(False, "Nada atiende en 192.168.1.10:8000: el maestro está detenido")
    assert m.status().detail == "En marcha (pid 4242). Nada atiende en 192.168.1.10:8000: el maestro está detenido"


def test_the_node_program_notes_when_it_is_cut_off_and_back(client, f, tmp_path, monkeypatch):
    from tisma_cloud.node.agent import Agent
    from tisma_cloud.node.config import NodeConfig

    token = f.node("n", "slice")["Authorization"].split(" ", 1)[1]
    path = tmp_path / STATE_FILE
    agent = Agent(NodeConfig(master_url="http://testserver", token=token, work_dir=tmp_path / "work", gpu=False),
                  client=client, state_path=path)
    agent.heartbeat()
    assert read_state(path)["ok"] is True
    real_post = client.post

    def refused(url, *args, **kwargs):
        raise wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))

    monkeypatch.setattr(client, "post", refused)
    with pytest.raises(httpx.ConnectError) as ex:
        agent.heartbeat()
    assert "Nada atiende" in agent.cut_off(ex.value) and read_state(path)["ok"] is False
    monkeypatch.setattr(client, "post", real_post)
    assert agent.claim(wait=0) is None
    assert read_state(path) == {**read_state(path), "ok": True, "message": "Conectado al maestro http://testserver"}


def test_the_master_says_why_each_node_does_not_work(client, f):
    """The master's window and panel show a reason next to each node, never a bare «sin señal»."""
    from datetime import timedelta

    from tisma_cloud.models import Node, utcnow
    from tisma_cloud.node_problems import node_problem

    admin = f.user("root@x.com", admin=True)
    now = utcnow()
    assert "Nunca se conectó" in node_problem(True, None, False, now)
    assert "Sin señal hace 12 min" in node_problem(True, now - timedelta(minutes=12), False, now)
    assert "Sin señal hace 3 h" in node_problem(True, now - timedelta(hours=3), False, now)
    assert "Deshabilitado" in node_problem(False, now, True, now)
    assert "Dos programas" in node_problem(True, now, True, now, twins=True)
    assert "versión 0.0.1 y este maestro la 9.9" in node_problem(True, now, True, now, "0.0.1", master_version="9.9")
    assert node_problem(True, now, True, now, "9.9", master_version="9.9") == ""

    f.node("nuevo", "slice")
    f.node("viejo", "slice")
    with f.db.session() as s:
        s.query(Node).filter(Node.name == "viejo").update({"last_seen": now - timedelta(minutes=30)})
    nodes = {n["name"]: n for n in client.get("/v1/admin/nodes", headers=admin).json()["nodes"]}
    assert "Nunca se conectó" in nodes["nuevo"]["problem"]
    assert "Sin señal hace 30 min" in nodes["viejo"]["problem"]


def test_the_master_notes_when_a_node_came_back_by_itself(client, f, settings, tmp_path):
    """After a silence, or after the master itself restarted, a node's first request is a reconnection: the panel and
    the window say «Reconectado hace N s». Its first connection ever is not one."""
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from tisma_cloud.api import create_app
    from tisma_cloud.models import Node, utcnow
    from tisma_cloud.node_problems import reconnected_text

    admin = f.user("root@x.com", admin=True)
    node = f.node("n", "slice")
    beat = lambda c: c.post("/v1/node/heartbeat", headers=node, json={"running": []})  # noqa: E731
    assert beat(client).status_code == 200
    view = lambda c: c.get("/v1/admin/nodes", headers=admin).json()["nodes"][0]  # noqa: E731
    assert view(client)["reconnected_at"] is None
    beat(client)
    assert view(client)["reconnected_at"] is None
    # Silent for a minute (the node was cut), then heard again.
    with f.db.session() as s:
        s.query(Node).update({"last_seen": utcnow() - timedelta(minutes=1)})
    beat(client)
    first = view(client)["reconnected_at"]
    assert first and reconnected_text(first).startswith("Reconectado hace ")
    # The master restarted (another process on the same database): the node's next request is a reconnection.
    with f.db.session() as s:
        s.query(Node).update({"reconnected_at": None})
    with TestClient(create_app(settings), client=("192.168.1.20", 50000)) as restarted:
        assert beat(restarted).status_code == 200
        assert view(restarted)["reconnected_at"] is not None
    assert reconnected_text("") == "" and reconnected_text("2000-01-01T00:00:00") == ""
