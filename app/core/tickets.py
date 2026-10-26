"""Versioned, signed check-in tickets (spec §11).

    payload = registration_id (16 bytes) || ticket_version (4 bytes, big-endian)
    token   = "cp1." + b64url(payload) + "." + b64url(HMAC-SHA256(TICKET_SECRET, "cp1." + b64url(payload)))

* Nothing secret is stored in the database: a DB leak alone cannot create tickets.
* The same (registration, version) always gives the same token, so viewing a ticket
  twice (JSON or PNG) never invalidates a screenshot.
* Invalidating = ticket_version + 1 (on reissue and on cancellation).
* TICKET_SECRET is separate from JWT_SECRET (key separation).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import re
import uuid

import qrcode

from app.core.config import get_settings

TOKEN_PREFIX = "cp1"
_PAYLOAD_LENGTH = 20  # 16-byte UUID + 4-byte version
_PAYLOAD_TEXT_LENGTH = 27  # canonical unpadded Base64URL encoding of 20 bytes
_MAC_LENGTH = 32  # SHA-256 HMAC digest
_MAC_TEXT_LENGTH = 43  # canonical unpadded Base64URL encoding of 32 bytes
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_VERSION = 2**31 - 1


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Decode only canonical, unpadded Base64URL text used by our ticket format."""
    if not _B64URL_RE.fullmatch(text):
        raise ValueError("invalid Base64URL alphabet or padding")
    padded = text + "=" * (-len(text) % 4)
    # validate=True rejects invalid characters; the regex above additionally rejects
    # standard Base64 '+'/'/' and '=' padding, which urlsafe_b64decode would tolerate.
    decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    if _b64url_encode(decoded) != text:
        raise ValueError("noncanonical Base64URL encoding")
    return decoded


def _sign(signed_part: str) -> bytes:
    key = get_settings().ticket_secret.encode("utf-8")
    return hmac.new(key, signed_part.encode("ascii"), hashlib.sha256).digest()


def build_ticket(registration_id: uuid.UUID, version: int) -> str:
    """Create the ticket token for one registration at one version."""
    if not 1 <= version <= _MAX_VERSION:
        raise ValueError("ticket version out of range")
    payload = registration_id.bytes + version.to_bytes(4, "big")
    signed_part = f"{TOKEN_PREFIX}.{_b64url_encode(payload)}"
    return f"{signed_part}.{_b64url_encode(_sign(signed_part))}"


def parse_ticket(token: str) -> tuple[uuid.UUID, int] | None:
    """Return (registration_id, version) if the token is well-formed AND correctly signed.

    Runs before any database access, so garbage tokens are rejected cheaply.
    """
    if not token.isascii():
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    # Exact lengths prevent oversized input and make the wire format unambiguous.
    if len(parts[1]) != _PAYLOAD_TEXT_LENGTH or len(parts[2]) != _MAC_TEXT_LENGTH:
        return None
    try:
        payload = _b64url_decode(parts[1])
        mac = _b64url_decode(parts[2])
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if len(payload) != _PAYLOAD_LENGTH or len(mac) != _MAC_LENGTH:
        return None
    version = int.from_bytes(payload[16:], "big")
    if not 1 <= version <= _MAX_VERSION:
        return None
    expected = _sign(f"{parts[0]}.{parts[1]}")
    if not hmac.compare_digest(mac, expected):  # constant time: no timing leak
        return None
    return uuid.UUID(bytes=payload[:16]), version


def render_qr_png(token: str) -> bytes:
    """QR code PNG for a token. Called only AFTER the transaction commits (§8 Rule 5)."""
    image = qrcode.make(token)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
