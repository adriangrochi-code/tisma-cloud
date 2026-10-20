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
    # Page where users create their account and pay the plan (shown by Tisma); empty: public_url + "/cuenta".
    billing_url: str = field(default_factory=lambda: os.environ.get("TISMA_BILLING_URL", ""))
    # Address of the master on internet (e.g. https://tismaservidores.net), for the pages and the payment provider.
    public_url: str = field(default_factory=lambda: os.environ.get("TISMA_PUBLIC_URL", "").rstrip("/"))
    # Payment provider of the account page: "" (none: payments recorded by the administrator) or "test" (a fake
    # provider to try the whole flow; never in production).
    billing_provider: str = field(default_factory=lambda: os.environ.get("TISMA_BILLING_PROVIDER", ""))
    # Protection of the master (guard.py): an address with this many refused requests in a minute (bad tokens, unknown
    # pages, limits) is blocked for ban_minutes; requests per minute of an address to the pages without account; uploads
    # (projects and results) received at the same time, in all and per account.
    ban_threshold: int = field(default_factory=lambda: _int("TISMA_BAN_THRESHOLD", 30))
    ban_minutes: int = field(default_factory=lambda: _int("TISMA_BAN_MINUTES", 10))
    anonymous_per_minute: int = field(default_factory=lambda: _int("TISMA_ANONYMOUS_PER_MINUTE", 120))
    max_uploads: int = field(default_factory=lambda: _int("TISMA_MAX_UPLOADS", 8))
    max_uploads_per_account: int = field(default_factory=lambda: _int("TISMA_MAX_UPLOADS_PER_ACCOUNT", 2))
    # E-mail of the codes (account confirmation, new password): the SMTP server of the domain (e.g. the mail hosting of
    # tisma.com.uy). Empty host: the messages are only written to the log (development).
    smtp_host: str = field(default_factory=lambda: os.environ.get("TISMA_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("TISMA_SMTP_PORT", 587))
    # starttls (port 587), ssl (port 465) or none (only for a server on the same machine).
    smtp_security: str = field(default_factory=lambda: os.environ.get("TISMA_SMTP_SECURITY", "starttls"))
    smtp_user: str = field(default_factory=lambda: os.environ.get("TISMA_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("TISMA_SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("TISMA_SMTP_FROM", "Tisma Cloud <noreply@tisma.com.uy>"))
    # Client IDs of "Sign in with Google" (web page, phone app), comma separated. Empty: disabled.
    google_client_ids: str = field(default_factory=lambda: os.environ.get("TISMA_GOOGLE_CLIENT_IDS", ""))
    # Google Play subscriptions (the phone app): package name of the app, JSON key file of the service account with access
    # to the Play Console, and the products of Play with their plan ("tisma_basico:basico,tisma_pro:pro"). Empty package:
    # disabled.
    play_package: str = field(default_factory=lambda: os.environ.get("TISMA_PLAY_PACKAGE", ""))
    play_credentials: str = field(default_factory=lambda: os.environ.get("TISMA_PLAY_CREDENTIALS", ""))
    play_products: str = field(default_factory=lambda: os.environ.get("TISMA_PLAY_PRODUCTS", ""))
    # Real-time developer notifications (Pub/Sub push to /v1/billing/play/notify): the audience set on the push
    # subscription and its service account. Empty: notifications refused.
    play_push_audience: str = field(default_factory=lambda: os.environ.get("TISMA_PLAY_PUSH_AUDIENCE", ""))
    play_push_account: str = field(default_factory=lambda: os.environ.get("TISMA_PLAY_PUSH_ACCOUNT", ""))
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
