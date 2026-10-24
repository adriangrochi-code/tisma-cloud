"""The machines of the local network that ask to be nodes (pairing.py, node/join.py) and the removal of a node."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tisma_cloud import cli, pairing
from tisma_cloud.manager import MASTER_NODE, Manager
from tisma_cloud.models import Job, Node, NodeRequest, utcnow
from tisma_cloud.node import __main__ as node_main
from tisma_cloud.node.agent import Agent
from tisma_cloud.node.config import NodeConfig
from tisma_cloud.node.join import Rejected, ask_to_join, request_path, write_token

INFO = {"host": "taller-pc", "cpus": 8, "memory_gb": 15.6, "os": "Windows-10"}


def join(client, name: str = "Taller PC", info: dict | None = None) -> dict:
    r = client.post("/v1/node/join", json={"name": name, "info": info or INFO})
    assert r.status_code == 201, r.text
    return r.json()


def state(client, answer: dict, secret: str | None = None):
    return client.get(f"/v1/node/join/{answer['id']}", headers={"X-Join-Secret": secret or answer["secret"]})


def test_a_machine_of_the_network_joins(client, f):
    admin = f.user("root@x.com", admin=True)
    asked = join(client)
    assert asked["poll_s"] == 5
    assert state(client, asked).json() == {"status": "pending"}
    # Only the machine that asked knows its secret.
    assert state(client, asked, "wrong").status_code == 404
    requests = client.get("/v1/admin/node-requests", headers=admin).json()["requests"]
    assert [(r["name"], r["address"], r["info"]["cpus"]) for r in requests] == [("Taller-PC", "192.168.1.20", 8)]
    r = client.post(f"/v1/admin/node-requests/{asked['id']}/accept", headers=admin, json={})
    assert r.status_code == 200 and r.json()["node"]["name"] == "Taller-PC"
    # All the job classes, and what the machine reported.
    assert r.json()["node"]["priorities"] == ["slice", "slice_large", "fea", "fea_optimize"]
    assert r.json()["node"]["info"]["host"] == "taller-pc"
    answer = state(client, asked).json()
    assert answer["status"] == "accepted" and answer["name"] == "Taller-PC" and answer["token"].startswith("tn_")
    # The token works, and is given only once.
    assert client.post("/v1/node/claim", headers={"Authorization": f"Bearer {answer['token']}"}, json={}).status_code == 204
    assert state(client, asked).status_code == 404
    assert client.get("/v1/admin/node-requests", headers=admin).json()["requests"] == []


def test_join_only_from_the_local_network(client):
    with TestClient(client.app, client=("200.40.10.7", 40000)) as internet:
        assert internet.post("/v1/node/join", json={"name": "x"}).status_code == 404
        assert internet.get("/v1/node/join/abc", headers={"X-Join-Secret": "s"}).status_code == 404
    # Through Caddy with the address of an internet user.
    r = client.post("/v1/node/join", json={"name": "x"}, headers={"X-Forwarded-For": "200.40.10.7"})
    assert r.status_code == 404


def test_the_same_machine_asking_again_waits_for_the_same_request(client, f):
    first, second = join(client), join(client)
    assert first["id"] == second["id"]
    # The new secret replaces the old one.
    assert state(client, first).status_code == 404 and state(client, second).json()["status"] == "pending"
    other = join(client, "otra")
    assert other["id"] != first["id"]


def test_a_rejected_machine_is_told_once(client, f):
    admin = f.user("root@x.com", admin=True)
    asked = join(client)
    assert client.post(f"/v1/admin/node-requests/{asked['id']}/reject", headers=admin).status_code == 200
    assert state(client, asked).json() == {"status": "rejected"}
    assert state(client, asked).status_code == 404
    assert client.post(f"/v1/admin/node-requests/{asked['id']}/accept", headers=admin, json={}).status_code == 404


def test_taken_names_and_old_requests(client, f):
    db = client.app.state.services.db
    f.node("Taller-PC")
    asked, other = join(client), join(client, "b")
    with db.session() as s:
        assert pairing.accept(s, asked["id"]).name == "Taller-PC-2"
        with pytest.raises(pairing.PairingError, match="each once"):
            pairing.accept(s, other["id"], priorities=["slice", "slice"])
    join(client, "c")
    with db.session() as s:
        assert pairing.drop_old(s, utcnow() + pairing.VALIDITY + timedelta(minutes=1)) == 3
        assert s.query(NodeRequest).count() == 0


def test_too_many_requests(client, monkeypatch):
    monkeypatch.setattr(pairing, "MAX_PENDING", 2)
    join(client, "a")
    join(client, "b")
    assert client.post("/v1/node/join", json={"name": "c"}).status_code == 429


def test_removing_a_node_requeues_its_jobs(client, f):
    admin = f.user("root@x.com", admin=True)
    user = f.user()
    node = f.node("n", "slice")
    done = f.submit(user)
    f.claim(node)
    client.post(f"/v1/node/jobs/{done['id']}/fail", headers=node, json={"error": "x"})
    running = f.submit(user)
    assert f.claim(node)["id"] == running["id"]
    r = client.delete("/v1/admin/nodes/n", headers=admin)
    assert r.status_code == 200 and r.json() == {"deleted": "n", "requeued": 1}
    assert client.get(f"/v1/jobs/{running['id']}", headers=user).json()["status"] == "queued"
    db = client.app.state.services.db
    with db.session() as s:
        assert s.get(Job, done["id"]).node_id is None and s.query(Node).count() == 0
    # The machine is no longer known: 401 makes it ask to join again.
    assert client.post("/v1/node/claim", headers=node, json={}).status_code == 401
    assert client.delete("/v1/admin/nodes/n", headers=admin).status_code == 404
    with TestClient(client.app, client=("200.40.10.7", 40000)) as internet:
        assert internet.delete("/v1/admin/nodes/x", headers=admin).status_code == 404
        assert internet.get("/v1/admin/node-requests", headers=admin).status_code == 404


def node_config(tmp_path, token: str = "") -> tuple[NodeConfig, object]:
    path = tmp_path / "node.toml"
    path.write_text(f'master_url = "http://testserver"\ntoken = "{token}"\ntisma_slicer = "s"\ntisma_fea = "f"\n'
                    f'name = "Taller PC"\nslots = 1\nwork_dir = "{(tmp_path / "work").as_posix()}"\n', encoding="utf-8")
    return NodeConfig.load(path), path


def test_the_node_asks_and_takes_its_token(client, f, tmp_path):
    admin = f.user("root@x.com", admin=True)
    cfg, path = node_config(tmp_path)
    waits = []

    def sleep(seconds: float) -> None:
        # The administrator accepts while the machine waits.
        waits.append(seconds)
        assert request_path(path).is_file()
        for r in client.get("/v1/admin/node-requests", headers=admin).json()["requests"]:
            client.post(f"/v1/admin/node-requests/{r['id']}/accept", headers=admin, json={"name": "i7-taller"})

    token = ask_to_join(cfg, path, client=client, sleep=sleep)
    assert waits == [5.] and token.startswith("tn_")
    assert NodeConfig.load(path).token == token and not request_path(path).exists()
    # The rest of node.toml is kept.
    assert 'name = "Taller PC"' in path.read_text(encoding="utf-8")
    assert client.post("/v1/node/claim", headers={"Authorization": f"Bearer {token}"}, json={}).status_code == 204
    assert [n["name"] for n in client.get("/v1/admin/nodes", headers=admin).json()["nodes"]] == ["i7-taller"]


def test_the_node_stops_when_rejected(client, f, tmp_path):
    admin = f.user("root@x.com", admin=True)
    cfg, path = node_config(tmp_path)

    def sleep(_seconds: float) -> None:
        for r in client.get("/v1/admin/node-requests", headers=admin).json()["requests"]:
            client.post(f"/v1/admin/node-requests/{r['id']}/reject", headers=admin)

    with pytest.raises(Rejected):
        ask_to_join(cfg, path, client=client, sleep=sleep)
    assert not request_path(path).exists() and NodeConfig.load(path).token == ""


def test_a_removed_node_forgets_its_token_and_asks_again(client, f, tmp_path, monkeypatch):
    admin = f.user("root@x.com", admin=True)
    token = f.node("n", "slice")["Authorization"].split(" ", 1)[1]
    client.delete("/v1/admin/nodes/n", headers=admin)
    cfg, path = node_config(tmp_path, token)
    agent = Agent(cfg, client=client)
    agent.run_forever()
    assert agent.unknown_token
    # The service of the node: it forgets the token, asks again and stops when rejected.
    asked = []
    monkeypatch.setattr(node_main, "Agent", lambda c: Agent(c, client=client))

    def ask(c, p):  # noqa: ANN001
        asked.append(NodeConfig.load(p).token)
        raise Rejected()

    monkeypatch.setattr(node_main, "ask_to_join", ask)
    assert node_main.main(["--config", str(path)]) == 0
    assert asked == [""]
    # The node of the master never asks: its token is made by the master.
    write_token(path, token)
    assert node_main.main(["--config", str(path), "--no-join"]) == 1


def test_disabled_node_is_not_asked_to_join(client, f):
    admin = f.user("root@x.com", admin=True)
    node = f.node("n", "slice")
    client.patch("/v1/admin/nodes/n", headers=admin, json={"enabled": False})
    assert client.post("/v1/node/claim", headers=node, json={}).status_code == 403


def test_write_token(tmp_path):
    path = tmp_path / "node.toml"
    path.write_text('master_url = "x"\ntoken = ""\nslots = 2', encoding="utf-8")
    write_token(path, "tn_a")
    assert path.read_text(encoding="utf-8") == 'master_url = "x"\ntoken = "tn_a"\nslots = 2'
    path.write_text('master_url = "x"', encoding="utf-8")
    write_token(path, "tn_b")
    assert path.read_text(encoding="utf-8") == 'master_url = "x"\ntoken = "tn_b"\n'


def test_the_window_accepts_rejects_and_removes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TISMA_DATABASE_URL", raising=False)
    monkeypatch.delenv("TISMA_STORAGE_DIR", raising=False)
    m = Manager(tmp_path)
    m.set_role("master")
    m.write_env({"TISMA_DATABASE_URL": f"sqlite:///{tmp_path / 'db.sqlite'}"})
    db, _settings = m._database()
    with db.session() as s:
        first = pairing.request_join(s, "taller", "192.168.1.30", INFO)[0]
        second = pairing.request_join(s, "oficina", "192.168.1.31", {})[0]
    assert [r["name"] for r in m.join_requests()] == ["taller", "oficina"]
    assert [r["name"] for r in m.dashboard()["requests"]] == ["taller", "oficina"]
    assert m.accept_join(first, "i7") == "i7"
    m.reject_join(second)
    assert m.join_requests() == [] and [n["name"] for n in m.nodes()] == ["i7"]
    with pytest.raises(pairing.PairingError):
        m.reject_join(second)
    # By hand: the token is shown once; the same name twice is refused.
    assert m.add_node("i5 taller").startswith("tn_")
    with pytest.raises(ValueError, match="i5-taller"):
        m.add_node("i5-taller")
    assert m.delete_node("i7") == 0
    assert [n["name"] for n in m.nodes()] == ["i5-taller"]
    # The node of the master is not removed while the master computes.
    with db.session() as s:
        s.add(Node(name=MASTER_NODE, token_hash="x" * 64))
    with pytest.raises(ValueError, match="El maestro también calcula"):
        m.delete_node(MASTER_NODE)
    m.set_master_computes(False)
    m.delete_node(MASTER_NODE)
    # The console of the master.
    with db.session() as s:
        third = pairing.request_join(s, "deposito", "192.168.1.32", {})[0]
    code, out = cli.admin_output(m, ["node-requests"])
    assert code == 0 and third in out and "deposito" in out
    code, out = cli.admin_output(m, ["node-accept", third, "--priorities", "fea,slice"])
    assert code == 0 and "deposito" in out
    assert {n["name"]: n["priorities"] for n in m.nodes()}["deposito"] == ["fea", "slice"]
    code, out = cli.admin_output(m, ["node-del", "deposito"])
    assert code == 0 and "removed" in out
    code, _out = cli.admin_output(m, ["node-del", "deposito"])
    assert code == 1
