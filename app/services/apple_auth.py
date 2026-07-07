import hashlib
import hmac
import time

import requests
from jose import JWTError, jwt

from app.config import settings


APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
_keys_cache: tuple[float, list[dict]] | None = None


class AppleTokenError(ValueError):
    pass


def _get_apple_keys() -> list[dict]:
    global _keys_cache
    now = time.monotonic()
    if _keys_cache is not None and now - _keys_cache[0] < 3600:
        return _keys_cache[1]

    try:
        response = requests.get(APPLE_KEYS_URL, timeout=5)
        response.raise_for_status()
        keys = response.json()["keys"]
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        raise AppleTokenError("Unable to fetch Apple signing keys") from exc

    _keys_cache = (now, keys)
    return keys


def verify_apple_identity_token(identity_token: str, raw_nonce: str) -> dict:
    try:
        header = jwt.get_unverified_header(identity_token)
    except JWTError as exc:
        raise AppleTokenError("Invalid Apple identity token") from exc

    if header.get("alg") != "RS256" or not header.get("kid"):
        raise AppleTokenError("Unsupported Apple identity token")

    signing_key = next(
        (key for key in _get_apple_keys() if key.get("kid") == header["kid"]),
        None,
    )
    if signing_key is None:
        # Apple may have rotated its keys since our cached fetch.
        global _keys_cache
        _keys_cache = None
        signing_key = next(
            (key for key in _get_apple_keys() if key.get("kid") == header["kid"]),
            None,
        )
    if signing_key is None:
        raise AppleTokenError("Apple signing key not found")

    try:
        claims = jwt.decode(
            identity_token,
            signing_key,
            algorithms=["RS256"],
            audience=settings.apple_client_id,
            issuer=APPLE_ISSUER,
        )
    except JWTError as exc:
        raise AppleTokenError("Apple identity token verification failed") from exc

    expected_nonce = hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(claims.get("nonce", "")), expected_nonce):
        raise AppleTokenError("Apple nonce verification failed")
    if not claims.get("sub"):
        raise AppleTokenError("Apple identity token has no subject")
    return claims
