"""Settings of the master server, from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Private networks (RFC 1918, loopback, IPv6 unique local and link local): the local network of the servers.
PRIVATE_NETWORKS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7,fe80::/10"
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("TISMA_DATABASE_URL", "sqlite:///./tisma_cloud.db"))
    storage_dir: Path = field(default_factory=lambda: Path(os.environ.get("TISMA_STORAGE_DIR", "./storage")))
    # Largest project accepted [MB].
    max_upload_mb: int = field(default_factory=lambda: _int("TISMA_MAX_UPLOAD_MB", 200))
    # Largest result a node may upload [MB] (G-code of big plates).
    max_result_mb: int = field(default_factory=lambda: _int("TISMA_MAX_RESULT_MB", 1024))
    # A node that has not been seen for this long has lost its running jobs: they go back to the queue.
    node_timeout_s: int = field(default_factory=lambda: _int("TISMA_NODE_TIMEOUT_S", 90))
    # Times a job is started before it fails (nodes lost while running it).
    max_attempts: int = field(default_factory=lambda: _int("TISMA_MAX_ATTEMPTS", 3))
    # How long a node waits in a claim for a job before the master answers "nothing" [s].
    claim_wait_s: int = field(default_factory=lambda: _int("TISMA_CLAIM_WAIT_S", 20))
    # Plates with more triangles are "large slicing" jobs (provisional threshold until measured on the machines).
    large_slice_triangles: int = field(default_factory=lambda: _int("TISMA_LARGE_SLICE_TRIANGLES", 300_000))
    # Results and projects older than this are deleted [h].
    retention_h: int = field(default_factory=lambda: _int("TISMA_RETENTION_H", 72))
    # Networks from which the administration (/admin and /v1/admin) can be used: the local network only. From any other
    # address those pages do not exist (404).
    admin_networks: str = field(default_factory=lambda: os.environ.get("TISMA_ADMIN_NETWORKS", PRIVATE_NETWORKS))
    # Addresses of the web site allowed to use /v1/site (comma separated, e.g. its server's public IP); empty: any address
    # with the site key.
    site_networks: str = field(default_factory=lambda: os.environ.get("TISMA_SITE_NETWORKS", ""))
    # Page of the web site where users create their account and pay the plan (shown by Tisma).
    billing_url: str = field(default_factory=lambda: os.environ.get("TISMA_BILLING_URL", ""))
    # Key of the web site for /v1/site (it creates the users and records their payments). Empty: disabled.
    site_key: str = field(default_factory=lambda: os.environ.get("TISMA_SITE_KEY", ""))
    # Days a user may keep computing after paid_until (a payment that arrives late).
    grace_days: int = field(default_factory=lambda: _int("TISMA_GRACE_DAYS", 3))
    # Login attempts per minute from one address and for one e-mail.
    login_per_minute: int = field(default_factory=lambda: _int("TISMA_LOGIN_PER_MINUTE", 10))
    # Requests per minute of a computing node (claims, progress, heartbeats).
    node_requests_per_minute: int = field(default_factory=lambda: _int("TISMA_NODE_REQUESTS_PER_MINUTE", 600))

    def __post_init__(self) -> None:
        self.storage_dir = Path(self.storage_dir)
