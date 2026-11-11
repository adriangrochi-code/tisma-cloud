#!/usr/bin/env python3
"""The translation tables of the profile pack, for the package of tisma-cloud (CI, job executable).

    python deploy/fetch_translation.py --out dist/tisma-profiles [--base URL]

The pack is the public repository adriangrochi-code/Tisma-profiles- (AGPL-3.0-or-later data). Its docs/FORMAT.md
(«Descarga», «Traducción de claves») says each release carries index.json and translation.tar.xz, the tables
translation/<dialect>.json, with the archive's size and SHA-256 in index.json (translation_archive) and the path of
each dialect's table (translation). This downloads both from the latest release, checks the archive against the index,
and unpacks only those tables into <out>/translation, with the pack's LICENSE and a SOURCE.txt that says which release
it is. tisma-engine reads them with --translation-dir to translate the settings of other slicers' projects.

Exit code 0 when every table of the index is in place."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import httpx

REPO = "adriangrochi-code/Tisma-profiles-"
BASE = f"https://github.com/{REPO}/releases/latest/download"
MAX_TABLE = 20 << 20  # bytes of one table


def fetch(client: httpx.Client, url: str) -> bytes:
    r = client.get(url)
    r.raise_for_status()
    return r.content


def tables(index: dict) -> dict[str, str]:
    """The dialects and their paths, refused when a path is not translation/<name>.json."""
    out = {}
    for dialect, path in (index.get("translation") or {}).items():
        p = PurePosixPath(str(path))
        if len(p.parts) != 2 or p.parts[0] != "translation" or not re.fullmatch(r"[a-z0-9_-]+\.json", p.name):
            raise ValueError(f"the index names a table out of translation/: {path!r}")
        out[str(dialect)] = str(p)
    if not out:
        raise ValueError("the index names no translation table")
    return out


def unpack(archive: bytes, wanted: set[str], dest: Path) -> None:
    """Only regular files whose path is one of the index's tables (no links, no other paths)."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:xz") as tar:
        for member in tar.getmembers():
            name = str(PurePosixPath(member.name.removeprefix("./")))
            if member.isdir():
                continue
            if name not in wanted:
                continue
            if not member.isfile() or member.size > MAX_TABLE:
                raise ValueError(f"{member.name}: not a regular file of at most {MAX_TABLE >> 20} MB")
            data = tar.extractfile(member).read()
            table = json.loads(data)
            if table.get("format") != "tisma-pack-translation":
                raise ValueError(f"{name}: format {table.get('format')!r}, not tisma-pack-translation")
            target = dest / PurePosixPath(name).name
            target.write_bytes(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="the folder of the pack in the package (tisma-profiles)")
    ap.add_argument("--base", default=BASE, help="where the release's files are (the latest release by default)")
    a = ap.parse_args()
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        index = json.loads(fetch(client, f"{a.base}/index.json"))
        if index.get("format") != "tisma-profile-pack" or int(index.get("version", 0)) > 2:
            raise SystemExit(f"index.json: format {index.get('format')!r} version {index.get('version')!r}")
        info = index.get("translation_archive") or {}
        archive = fetch(client, f"{a.base}/{info.get('name', 'translation.tar.xz')}")
        if len(archive) != int(info.get("size", -1)) or hashlib.sha256(archive).hexdigest() != info.get("sha256"):
            raise SystemExit("translation.tar.xz does not match the size and SHA-256 of index.json")
        wanted = tables(index)
        commit = str(index.get("pack_version", "")).rpartition("-")[2]
        licence = fetch(client, f"https://raw.githubusercontent.com/{REPO}/{commit or 'main'}/LICENSE")
    with tempfile.TemporaryDirectory(dir=a.out.parent if a.out.parent.exists() else None) as tmp:
        stage = Path(tmp) / "pack"
        (stage / "translation").mkdir(parents=True)
        unpack(archive, set(wanted.values()), stage / "translation")
        missing = [d for d, p in wanted.items() if not (stage / p).is_file()]
        if missing:
            raise SystemExit(f"tables missing in translation.tar.xz: {', '.join(missing)}")
        (stage / "LICENSE-AGPL-3.0.txt").write_bytes(licence)
        (stage / "SOURCE.txt").write_text(
            f"The translation tables of the Tisma profile pack, version {index.get('pack_version')} "
            f"(generated {index.get('generated', '?')}), {index.get('license', 'AGPL-3.0-or-later')}: "
            f"LICENSE-AGPL-3.0.txt.\nSource: https://github.com/{REPO}/tree/{commit or 'main'} "
            "(tools/translation.py; docs/FORMAT.md, «Traducción de claves»).\n",
            encoding="utf-8")
        shutil.rmtree(a.out, ignore_errors=True)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(a.out))
    print(f"{a.out}: {index.get('pack_version')}, {len(wanted)} tables ({', '.join(sorted(wanted))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
