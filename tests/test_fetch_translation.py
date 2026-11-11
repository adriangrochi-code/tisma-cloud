from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
import fetch_translation as ft  # noqa: E402

TABLE = json.dumps({"format": "tisma-pack-translation", "version": 1, "dialect": "orcaslicer", "kinds": {}}).encode()


def archive(files: dict[str, bytes], link: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


def test_only_the_tables_of_the_index(tmp_path):
    assert ft.tables({"translation": {"orcaslicer": "translation/orcaslicer.json"}}) == {
        "orcaslicer": "translation/orcaslicer.json"}
    for bad in ("../x.json", "translation/../x.json", "/translation/x.json", "vendors/x.json", "translation/x.txt"):
        with pytest.raises(ValueError):
            ft.tables({"translation": {"x": bad}})
    with pytest.raises(ValueError):
        ft.tables({})
    data = archive({"translation/orcaslicer.json": TABLE, "translation/extra.json": TABLE, "../evil.json": b"x"})
    dest = tmp_path / "tables"
    dest.mkdir()
    ft.unpack(data, {"translation/orcaslicer.json"}, dest)
    assert [p.name for p in dest.iterdir()] == ["orcaslicer.json"]
    with pytest.raises(ValueError, match="regular file"):
        ft.unpack(archive({}, link="translation/orcaslicer.json"), {"translation/orcaslicer.json"}, dest)
    with pytest.raises(ValueError, match="format"):
        ft.unpack(archive({"translation/orcaslicer.json": b"{}"}), {"translation/orcaslicer.json"}, dest)


def test_a_release_whose_archive_does_not_match_its_index(tmp_path, monkeypatch):
    data = archive({"translation/orcaslicer.json": TABLE})
    index = {"format": "tisma-profile-pack", "version": 2, "pack_version": "2026.11.10-abc1234",
             "translation_archive": {"name": "translation.tar.xz", "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()},
             "translation": {"orcaslicer": "translation/orcaslicer.json"}}
    served = {"index.json": json.dumps(index).encode(), "translation.tar.xz": data, "LICENSE": b"AGPL"}
    monkeypatch.setattr(ft, "fetch", lambda _client, url: served[url.rsplit("/", 1)[1]])
    out = tmp_path / "dist" / "tisma-profiles"
    monkeypatch.setattr(sys, "argv", ["fetch_translation.py", "--out", str(out)])
    assert ft.main() == 0
    assert (out / "translation" / "orcaslicer.json").read_bytes() == TABLE
    assert "2026.11.10-abc1234" in (out / "SOURCE.txt").read_text(encoding="utf-8")
    served["translation.tar.xz"] = data[:-1] + b"x"
    with pytest.raises(SystemExit, match="SHA-256"):
        ft.main()
