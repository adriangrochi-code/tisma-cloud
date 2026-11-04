"""3D scans of Tisma Scanner (the Android app of the openscanner repository) computed in the cloud.

A scan is a zip of photos and sensor data, from tens of MB to several GB: too large for one request (Cloudflare cuts
requests of more than 100 MB) and sent from a phone, whose connection may drop. So it is sent in pieces, as the app
already did with its own server:

    POST   /v1/scans                     {"size": bytes, "name": "..."}  -> {"id", "chunk_size", "received"}
    PUT    /v1/scans/{id}/data?offset=N  one piece (409 with "received" when the offset is not where it goes on)
    GET    /v1/scans/{id}                what has arrived, and the job when complete
    POST   /v1/scans/{id}/complete       checks the zip and makes the job (kind "scan"): the job of GET /v1/jobs/{id}
    DELETE /v1/scans/{id}                gives up a scan being sent

The job runs on a node with tisma_scan (photogrammetry with COLMAP; node/runners.py) and its result is a zip with the
part in STL (mm), its report and the point cloud. Scans being sent and not touched for a day are removed."""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath

# Pieces not touched for this long are removed with their upload [h].
STALE_H = 24
# The absolute position (latitude, longitude, altitude) never leaves the phone (owner, 2026-11-04): only the relative
# poses between the photos. A scan that carries it is refused, and not kept.
ABSOLUTE_POSITION = {"gps.csv"}
# Entries of a scan at most (photos, depth maps, sensor logs).
MAX_ENTRIES = 100_000


class ScanError(ValueError):
    pass


def upload_path(storage_root: Path, upload_id: str) -> Path:
    # Upload ids are UUIDs made by the server, never names sent by users.
    return Path(storage_root) / "scans" / f"{upload_id}.zip.part"


def check_scan(path: Path, max_unzipped: int) -> int:
    """Raises ScanError unless the file is a zip of Tisma Scanner (its session.json) with safe paths and a bounded
    uncompressed size. Returns the number of photos (JPEG) in it."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ScanError(f"The scan has more than {MAX_ENTRIES} files")
            total = 0
            photos = 0
            has_session = False
            for info in entries:
                name = info.filename
                parts = PurePosixPath(name.replace("\\", "/")).parts
                if name.startswith(("/", "\\")) or ".." in parts or ":" in name or "\0" in name:
                    raise ScanError(f"A path that is not allowed in the scan: {name[:200]}")
                total += info.file_size
                if total > max_unzipped:
                    raise ScanError(f"The scan is larger than {max_unzipped >> 30} GB uncompressed")
                lower = name.lower()
                if PurePosixPath(lower).name in ABSOLUTE_POSITION:
                    raise ScanError("The scan carries the absolute position of the phone (gps.csv): update Tisma "
                                    "Scanner, which no longer sends it")
                has_session = has_session or lower.endswith("session.json")
                photos += lower.endswith((".jpg", ".jpeg"))
    except (zipfile.BadZipFile, OSError):
        raise ScanError("The scan is not a zip file") from None
    if not has_session:
        raise ScanError("The file is not a scan of Tisma Scanner (session.json is missing)")
    if photos == 0:
        raise ScanError("The scan has no photos")
    return photos
