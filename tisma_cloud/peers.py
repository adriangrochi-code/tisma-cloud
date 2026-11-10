"""Who shares which model of the library, and the tickets of Tisma's relays (p2p_library.md 8 bis to 8 quinquies, with
the engine's choice of 2026-11-10: libp2p, Noise, circuit relay v2).

- A peer is an app, known by its Ed25519 peer key (the one that signs its publications); its libp2p PeerId is derived
  from that key.
- An app announces the models it shares and its addresses, signed by its peer key. The master keeps that only in
  memory for ANNOUNCE_TTL_S (an address is a personal datum: 8 quinquies) and hands out, for a model, the peers to
  contact: their own addresses only between two apps that chose «Directo», otherwise the address through a relay of
  Tisma (<relay>/p2p-circuit/p2p/<peer id>), so nobody else learns their IP.
- A relay accepts a reservation or a circuit only with a ticket the master signed with the library key: the account,
  the PeerId, its end and the cap per hour; it counts the bytes per account and needs no call to the master."""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass, field

from .library import LibraryKey, canonical

ANNOUNCE_TTL_S = 30 * 60
CLOCK_SKEW_S = 5 * 60
TICKET_TTL_S = 3600
MAX_ADDRS = 8
MAX_MODELS = 500
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def peer_id(public_key: bytes) -> str:
    """The libp2p PeerId of an Ed25519 key: the identity multihash of its protobuf PublicKey (KeyType Ed25519 = 1,
    then the 32 bytes), in base58btc ("12D3KooW...")."""
    proto = b"\x08\x01\x12\x20" + public_key
    return _base58(b"\x00" + bytes([len(proto)]) + proto)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def ticket(key: LibraryKey, account: str, public_key: bytes, cap_mb_per_hour: int, now: float | None = None) -> dict:
    """A relay ticket: base64url(payload) "." base64url(Ed25519 signature of the payload's bytes, library key)."""
    expires = int((now or time.time()) + TICKET_TTL_S)
    payload = canonical({"account": account, "peer_id": peer_id(public_key), "expires": expires,
                         "cap_mb_per_hour": cap_mb_per_hour})
    return {"ticket": _b64url(payload) + "." + _b64url(key.sign(payload)), "expires": expires}


def announcement_bytes(body: dict) -> bytes:
    """What the app's peer key signs to announce itself."""
    return canonical({k: body.get(k) for k in ("addrs", "direct", "models", "peer_key", "time")})


@dataclass
class Peer:
    peer_key: str
    account: str
    models: frozenset[str]
    addrs: tuple[str, ...]
    direct: bool
    seen: float = field(default_factory=time.monotonic)

    @property
    def id(self) -> str:
        return peer_id(base64.b64decode(self.peer_key))


class Announcements:
    """The peers that announced themselves lately, in memory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}

    def _forget_old(self, now: float) -> None:
        for key in [k for k, p in self._peers.items() if now - p.seen > ANNOUNCE_TTL_S]:
            del self._peers[key]

    def announce(self, peer: Peer) -> None:
        with self._lock:
            self._forget_old(peer.seen)
            self._peers[peer.peer_key] = peer

    def get(self, peer_key: str) -> Peer | None:
        with self._lock:
            self._forget_old(time.monotonic())
            return self._peers.get(peer_key)

    def forget(self, peer_key: str) -> None:
        with self._lock:
            self._peers.pop(peer_key, None)

    def providers(self, model: str) -> list[Peer]:
        with self._lock:
            self._forget_old(time.monotonic())
            return [p for p in self._peers.values() if model in p.models]


def addresses_for(provider: Peer, asker_direct: bool, relays: list[str]) -> list[str]:
    """The provider's own addresses only when both chose «Directo»; otherwise its address through each relay."""
    if provider.direct and asker_direct:
        return list(provider.addrs)
    return [f"{relay.rstrip('/')}/p2p-circuit/p2p/{provider.id}" for relay in relays]


def normal_ip(text: str) -> str | None:
    import ipaddress

    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def address_of(multiaddr: str) -> str | None:
    """The IP of a multiaddr that starts with /ip4/<ip>/ or /ip6/<ip>/ (None for any other: /dns4, a relay...)."""
    parts = multiaddr.split("/")
    if len(parts) >= 3 and parts[0] == "" and parts[1] in ("ip4", "ip6"):
        return normal_ip(parts[2])
    return None
