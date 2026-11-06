"""The errors of the HTTP client of a node, whichever library it is built on.

A node makes its own httpx client, but it can be given one (the tests give it Starlette's test client), and that
client may be built on httpx2, whose errors have the same names but are other classes. These tuples catch both."""

from __future__ import annotations

import httpx

try:
    import httpx2 as _httpx2
except ImportError:  # the usual case: only httpx
    _httpx2 = None


def _both(name: str) -> tuple[type[BaseException], ...]:
    other = getattr(_httpx2, name, None) if _httpx2 is not None else None
    return (getattr(httpx, name),) + ((other,) if isinstance(other, type) else ())


HTTPError = _both("HTTPError")
HTTPStatusError = _both("HTTPStatusError")
TransportError = _both("TransportError")
