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
    # The queue takes the shortest jobs first: a job waits at most this much longer than in arrival order [s].
    queue_size_wait_max_s: int = field(default_factory=lambda: _int("TISMA_QUEUE_SIZE_WAIT_MAX_S", 600))
    # Connections the master serves at once; past it, new ones get 503 at once. Each bridge and node waiting for work
    # keeps one open (see "Capacidad" in README.md).
    max_connections: int = field(default_factory=lambda: _int("TISMA_MAX_CONNECTIONS", 4000))
    # The log of every request (with the address of each visitor): off, so that the master does not keep who connected
    # from where. 1: on (to look into a problem for a while).
    access_log: bool = field(default_factory=lambda: os.environ.get("TISMA_ACCESS_LOG", "0") == "1")
    # ClamAV (clamd) that checks every file uploaded, "host:port" (the clamav service of compose.master.yaml:
    # clamav:3310). Empty: no antivirus (the projects and G-code are still checked, threats.py).
    clamd: str = field(default_factory=lambda: os.environ.get("TISMA_CLAMD", ""))
    # A session (a device logged in) not used for this long is closed [days].
    session_days: int = field(default_factory=lambda: _int("TISMA_SESSION_DAYS", 90))
    # The list of the jobs of an account (names of the files, times) is kept this long after they ended [days]; the
    # hours used stay in the monthly usage.
    job_history_days: int = field(default_factory=lambda: _int("TISMA_JOB_HISTORY_DAYS", 90))
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
    # Empty: http:// the dynamic DNS name (ddns_host) or the public IPv4 of the machine, and the port (publicip.py).
    public_url: str = field(default_factory=lambda: os.environ.get("TISMA_PUBLIC_URL", "").rstrip("/"))
    # Port where the master listens, set by the program that starts it (for the address made when public_url is empty).
    port: int = field(default_factory=lambda: _int("TISMA_PORT", 8000))
    # Where the master asks its public IPv4 (an answer of plain text).
    public_ip_url: str = field(default_factory=lambda: os.environ.get("TISMA_PUBLIC_IP_URL", "https://api.ipify.org"))
    # Dynamic DNS (the dyndns2 protocol of No-IP and others): a name kept pointing at the public IPv4 of the master, for
    # the internet connections whose address changes. Empty host, user or password: no updates.
    ddns_host: str = field(default_factory=lambda: os.environ.get("TISMA_DDNS_HOST", "").strip())
    ddns_user: str = field(default_factory=lambda: os.environ.get("TISMA_DDNS_USER", "").strip())
    ddns_password: str = field(default_factory=lambda: os.environ.get("TISMA_DDNS_PASSWORD", ""))
    ddns_url: str = field(default_factory=lambda: os.environ.get("TISMA_DDNS_URL",
                                                                  "https://dynupdate.no-ip.com/nic/update"))
    # E-mail of the one who runs this master, in the User-Agent the service asks for; empty: the user (No-IP: an e-mail).
    ddns_contact: str = field(default_factory=lambda: os.environ.get("TISMA_DDNS_CONTACT", "").strip())
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
    # Alerts for the administrators (alerts.py): e-mails (comma separated; empty: the administrator accounts), minutes
    # a node may be silent and a job may wait in the queue, free disk [GB], hours between reminders.
    alert_emails: str = field(default_factory=lambda: os.environ.get("TISMA_ALERT_EMAILS", ""))
    alert_node_minutes: int = field(default_factory=lambda: _int("TISMA_ALERT_NODE_MINUTES", 5))
    alert_queue_minutes: int = field(default_factory=lambda: _int("TISMA_ALERT_QUEUE_MINUTES", 30))
    alert_disk_free_gb: int = field(default_factory=lambda: _int("TISMA_ALERT_DISK_FREE_GB", 5))
    alert_repeat_h: int = field(default_factory=lambda: _int("TISMA_ALERT_REPEAT_H", 12))
    # Key of the web site for /v1/site (it creates the users and records their payments). Empty: disabled.
    site_key: str = field(default_factory=lambda: os.environ.get("TISMA_SITE_KEY", ""))
    # Internal accounts with unlimited computing, never charged (the team): e-mails and whole domains ("@tisma.com.uy"),
    # comma separated. An account with one of them is internal from its creation, and at each start of the master.
    internal_emails: str = field(default_factory=lambda: os.environ.get("TISMA_INTERNAL_EMAILS", ""))
    # Days a user may keep computing after paid_until (a payment that arrives late).
    grace_days: int = field(default_factory=lambda: _int("TISMA_GRACE_DAYS", 3))
    # Login attempts per minute from one address and for one e-mail.
    login_per_minute: int = field(default_factory=lambda: _int("TISMA_LOGIN_PER_MINUTE", 10))
    # Requests per minute of a computing node (claims, progress, heartbeats).
    node_requests_per_minute: int = field(default_factory=lambda: _int("TISMA_NODE_REQUESTS_PER_MINUTE", 600))
    # Remote printers: requests per minute of an account for its bridges and the phone (states, commands, camera
    # images), apart from the requests of its plan; minutes a command waits for its bridge before it fails.
    remote_requests_per_minute: int = field(default_factory=lambda: _int("TISMA_REMOTE_REQUESTS_PER_MINUTE", 600))
    remote_command_minutes: int = field(default_factory=lambda: _int("TISMA_REMOTE_COMMAND_MINUTES", 10))
    # Problems reported from Tisma and the app: per hour from an account and from an address without account, and how
    # long they are kept [days].
    reports_per_hour: int = field(default_factory=lambda: _int("TISMA_REPORTS_PER_HOUR", 20))
    anonymous_reports_per_hour: int = field(default_factory=lambda: _int("TISMA_ANONYMOUS_REPORTS_PER_HOUR", 5))
    report_days: int = field(default_factory=lambda: _int("TISMA_REPORT_DAYS", 365))
    # The reward of a useful report (given by an administrator): days of the plan of its author; at most this many
    # rewards per account in a year.
    # LibreTranslate on our own servers (the libretranslate service of compose.master.yaml, e.g.
    # http://libretranslate:5000) to read and search the reports in the language of the team; empty: not translated.
    translate_url: str = field(default_factory=lambda: os.environ.get("TISMA_TRANSLATE_URL", ""))
    translate_to: str = field(default_factory=lambda: os.environ.get("TISMA_TRANSLATE_TO", "es"))
    report_reward_days: int = field(default_factory=lambda: _int("TISMA_REPORT_REWARD_DAYS", 7))
    report_rewards_per_year: int = field(default_factory=lambda: _int("TISMA_REPORT_REWARDS_PER_YEAR", 4))

    def __post_init__(self) -> None:
        self.storage_dir = Path(self.storage_dir)

    def is_internal_email(self, email: str) -> bool:
        """Whether the e-mail is one of internal_emails, or of one of its domains."""
        email = email.strip().lower()
        for entry in (e.strip().lower() for e in self.internal_emails.split(",")):
            if entry and (email == entry or (entry.startswith("@") and email.endswith(entry))):
                return True
        return False
