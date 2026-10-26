"""Passwords (Argon2id + NIST policy) and access tokens (JWT, HS256 pinned). Spec §5.

Passwords
* Argon2id via pwdlib (OWASP's first choice): slow and memory-hard, so GPU guessing is expensive.
* NIST SP 800-63B-4 policy: 15-128 characters, blocklist of common passwords, no
  composition rules, Unicode NFC normalization.

Tokens
* The algorithm is HARD-CODED (RFC 8725): never taken from the token header.
* Required claims: exp, iat, sub, iss, aud, jti. No role inside the token:
  the role is read from the database on every request.
"""

from __future__ import annotations

import time
import unicodedata
import uuid
from functools import lru_cache
from pathlib import Path

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

JWT_ALGORITHM = "HS256"  # pinned in code, never configurable (RFC 8725 §3.1)
REQUIRED_CLAIMS = ["exp", "iat", "sub", "iss", "aud", "jti"]
JWT_LEEWAY_SECONDS = 10

PASSWORD_MIN_LENGTH = 15  # NIST SP 800-63B-4: single-factor passwords
PASSWORD_MAX_LENGTH = 128  # NIST asks for >= 64 to be allowed; the cap limits hashing cost

BLOCKLIST_PATH = Path(__file__).resolve().parent.parent / "data" / "common_passwords.txt"

_password_hash = PasswordHash.recommended()  # Argon2id with pwdlib's recommended parameters


# ---------------------------------------------------------------- passwords


@lru_cache(maxsize=1)
def load_password_blocklist() -> frozenset[str]:
    """Load the common-password list once. A missing file stops the app at startup."""
    if not BLOCKLIST_PATH.is_file():
        raise RuntimeError(
            f"Password blocklist not found at {BLOCKLIST_PATH}. "
            "It is required by the password policy (see README)."
        )
    entries: set[str] = set()
    with BLOCKLIST_PATH.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            word = line.strip()
            if word and not word.startswith("#"):
                entries.add(unicodedata.normalize("NFC", word).casefold())
    return frozenset(entries)


def normalize_password(password: str) -> str:
    """NFC normalization: the same visible password always hashes the same way."""
    return unicodedata.normalize("NFC", password)


def password_policy_issues(password: str, *, email: str = "", full_name: str = "") -> list[str]:
    """Return a list of human-readable problems (empty list = acceptable)."""
    pw = normalize_password(password)
    issues: list[str] = []
    if len(pw) < PASSWORD_MIN_LENGTH:
        issues.append(f"must be at least {PASSWORD_MIN_LENGTH} characters (a short phrase works well)")
    if len(pw) > PASSWORD_MAX_LENGTH:
        issues.append(f"must be at most {PASSWORD_MAX_LENGTH} characters")
    folded = pw.casefold()
    if folded in load_password_blocklist():
        issues.append("is in a list of commonly used passwords")

    # Context-specific words (NIST): the service name, the email name, the person's name.
    context_words = {"campuspass"}
    local_part = email.split("@", 1)[0].casefold()
    if len(local_part) >= 4:
        context_words.add(local_part)
    context_words.update(part.casefold() for part in full_name.split() if len(part) >= 4)
    if any(word in folded for word in context_words):
        issues.append("must not contain your name, your email name or the word 'campuspass'")
    return issues


def hash_password(password: str) -> str:
    return _password_hash.hash(normalize_password(password))


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hash.verify(normalize_password(password), password_hash)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """Hash used when the email does not exist, so login takes the same time either way."""
    return hash_password("campuspass-timing-equaliser-not-a-real-password")


# ---------------------------------------------------------------- tokens


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    """Return (token, expires_in_seconds). Uses the REAL clock (PyJWT validates real time)."""
    settings = get_settings()
    now = int(time.time())
    expires_in = settings.access_token_minutes * 60
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + expires_in,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": uuid.uuid4().hex,  # unique token id: enables a revocation denylist later
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=JWT_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> uuid.UUID:
    """Validate a token and return the user id. Raises jwt.PyJWTError or ValueError."""
    settings = get_settings()
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[JWT_ALGORITHM],  # hard-coded allow-list: blocks alg=none and alg confusion
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        leeway=JWT_LEEWAY_SECONDS,
        # PyJWT only checks exp/iat *if present*; "require" makes them mandatory.
        options={"require": REQUIRED_CLAIMS},
    )
    return uuid.UUID(str(claims["sub"]))  # ValueError if sub is not a UUID
