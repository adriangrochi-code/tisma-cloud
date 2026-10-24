"""Sign in with Google: the ID token the Google button (web page) or the Google SDK (phone app) gives is verified here
as Google documents it (developers.google.com/identity/gsi/web/guides/verify-google-id-token): signature with Google's
public keys, issuer, audience (our client IDs) and expiry, with PyJWT.

Google is the authority on the address only for Gmail addresses, or for Google Workspace accounts (hd set) whose
email_verified is true; only then is an existing account of that address linked automatically.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleError(Exception):
    pass


@dataclass
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    # Google is the authority on this address (Gmail, or Workspace with a verified address).
    authoritative: bool


class GoogleVerifier:
    def __init__(self, client_ids: list[str], certs_url: str = GOOGLE_CERTS, jwk_client=None) -> None:  # noqa: ANN001
        self.client_ids = client_ids
        # Keys cached by PyJWT; they rotate, so a key not in the cache is fetched again.
        self.jwk_client = jwk_client or jwt.PyJWKClient(certs_url, cache_keys=True, lifespan=3600)

    def verify(self, id_token: str) -> GoogleIdentity:
        if not self.client_ids:
            raise GoogleError("Sign in with Google is not enabled")
        try:
            key = self.jwk_client.get_signing_key_from_jwt(id_token).key
            claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=self.client_ids,
                                options={"require": ["iss", "aud", "exp", "sub"]}, leeway=30)
        except (jwt.PyJWTError, ValueError) as ex:
            raise GoogleError(f"Invalid Google token: {ex}") from None
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise GoogleError("Invalid Google token: issuer")
        email = str(claims.get("email", "")).strip().lower()
        if not email:
            raise GoogleError("The Google account has no e-mail")
        verified = claims.get("email_verified") in (True, "true")
        authoritative = verified and (email.endswith("@gmail.com") or bool(claims.get("hd")))
        return GoogleIdentity(str(claims["sub"]), email, verified, authoritative)
