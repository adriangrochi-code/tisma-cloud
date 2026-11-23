"""The log of the service: one line per event with the UTC date and the component, rotated by size, no secrets."""

from __future__ import annotations

import io
import re

from tisma_cloud import eventlog

LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[[^\]]+\] ")


def test_a_line_has_the_utc_date_and_the_component_and_no_secrets():
    text = eventlog.line("nodo", "Authorization: Bearer abcdefghijklmnop token=tu_" + "x" * 40 +
                         " node tn_" + "y" * 43 + " ?ticket=abc.def password: hunter22 secret=s3\nsecond line")
    assert LINE.match(text) and text.count("\n") == 0
    for secret in ("abcdefghijklmnop", "x" * 40, "y" * 43, "abc.def", "hunter22", "s3 "):
        assert secret not in text + " "
    assert "tn_***" in text and "Bearer ***" in text and "second line" in text
    # Ordinary words are kept.
    for kept in ("The key in storage/data.key is not the one this database was encrypted with",
                 "New key made in storage/data.key: keep a copy of it apart from the backups"):
        assert eventlog.line("maestro", kept).endswith(kept)
    said = eventlog.line("maestro", "TISMA_SMTP_PASSWORD=abc TISMA_SEED_TOKEN: tu_short relay_key=zz")
    assert "abc" not in said and "tu_short" not in said and "zz" not in said


def test_the_log_rotates_by_size_keeping_five(tmp_path):
    path = tmp_path / "logs" / "tisma-cloud.log"
    log = eventlog.RotatingLog(path, max_bytes=1000, backups=5)
    for i in range(400):
        log.write("servicio", f"event {i:04d} " + "z" * 40)
    log.close()
    files = sorted(p.name for p in path.parent.iterdir())
    assert files == ["tisma-cloud.log", *(f"tisma-cloud.log.{n}" for n in range(1, 6))]
    assert all(p.stat().st_size < 1100 for p in path.parent.iterdir())
    assert "event 0399" in path.read_text(encoding="utf-8")


def test_children_lines_and_prints_of_the_supervisor_go_to_the_log(tmp_path):
    path = tmp_path / "tisma-cloud.log"
    log = eventlog.RotatingLog(path)
    eventlog.pump(io.BytesIO(b"INFO:     Started server process [7]\nERROR:    puerto ocupado\n"), log,
                  "maestro")
    writer = eventlog.Writer(log, "servicio")
    print("[tisma-cloud] el nodo del maestro arranca (pid 9)", file=writer)
    eventlog.event(path, "servicio", "inicio pedido por botón")
    log.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert all(LINE.match(x) for x in lines)
    assert [x.split(" ", 1)[1] for x in lines] == [
        "[maestro] INFO:     Started server process [7]", "[maestro] ERROR:    puerto ocupado",
        "[servicio] el nodo del maestro arranca (pid 9)", "[servicio] inicio pedido por botón"]
