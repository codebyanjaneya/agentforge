"""ES256 access-token verification (D018). Public keys only: the backend can never mint a token.

Contract enforced here:
* header: ``alg`` is exactly ``ES256`` (the token's requested algorithm is never trusted; ``none`` and HS256
  are rejected before any key is used), ``typ`` is exactly ``JWT``, ``kid`` is present and names a
  configured public key; ``crit``, ``jku``, ``jwk``, ``x5u`` and ``x5c`` headers are rejected;
* the signature is verified (PyJWT) before any claim is trusted, with ``leeway`` of exactly 30 seconds;
* ``iss`` and ``aud`` equal the configured values exactly (``aud`` must be a single string);
* required: ``sub``, ``idp``, ``email``, ``email_verified``, ``jti``, ``iat``, ``exp``, ``token_use``; types are
  strict (integers for times, the boolean ``true`` for ``email_verified``), ``token_use`` is ``access``,
  ``idp`` is a supported provider, ``sub`` is a valid subject, and ``exp - iat`` is at most 600 seconds
  (the approved 10-minute lifetime; a longer-lived token is refused).

Provider and subject are NOT normalized: they are returned exactly as signed (no case folding, no trimming);
only validated. Email is an attribute and is never used as an identity. Failures raise ``AuthenticationError``
with a fixed reason code; no token, key or claim content is ever put in an exception or a log record.
"""

from dataclasses import dataclass
from typing import Mapping

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from auth.errors import AuthConfigurationError, AuthenticationError

ALGORITHM = "ES256"
TOKEN_TYPE = "JWT"
TOKEN_USE = "access"
LEEWAY_SECONDS = 30
MAX_LIFETIME_SECONDS = 600
SUPPORTED_PROVIDERS = frozenset({"google"})
MAX_TOKEN_CHARS = 4096
MAX_SUBJECT_CHARS = 255  # auth_identities.provider_subject is VARCHAR(255)
MAX_EMAIL_CHARS = 320  # users.email is VARCHAR(320)
MAX_JTI_CHARS = 128
FORBIDDEN_HEADERS = frozenset({"crit", "jku", "jwk", "x5u", "x5c"})
REQUIRED_CLAIMS = ("sub", "idp", "email", "email_verified", "jti", "iat", "exp", "token_use", "iss", "aud")


@dataclass(frozen=True)
class VerifiedClaims:
    """The claims of a token whose signature and contract were verified."""

    provider: str
    subject: str
    email: str
    jti: str
    issued_at: int
    expires_at: int


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    audience: str
    keys: Mapping[str, ec.EllipticCurvePublicKey]

    @classmethod
    def from_pem_keys(cls, issuer: str, audience: str, pem_by_kid: Mapping[str, str]) -> "AuthConfig":
        """Validate and parse the configuration; anything missing or invalid raises AuthConfigurationError."""
        if not isinstance(issuer, str) or not issuer.strip() or not isinstance(audience, str) or not audience.strip():
            raise AuthConfigurationError("issuer_or_audience_missing")
        if not pem_by_kid:
            raise AuthConfigurationError("no_public_keys")
        keys: dict[str, ec.EllipticCurvePublicKey] = {}
        for kid, pem in pem_by_kid.items():
            if not isinstance(kid, str) or not kid.strip() or not isinstance(pem, str):
                raise AuthConfigurationError("invalid_key_entry")
            try:
                key = serialization.load_pem_public_key(pem.encode("utf-8"))
            except Exception:  # noqa: BLE001 - never surface parser details (or key material)
                raise AuthConfigurationError("unparseable_public_key") from None
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
                raise AuthConfigurationError("public_key_not_p256")
            keys[kid] = key
        return cls(issuer=issuer, audience=audience, keys=keys)

    @classmethod
    def from_settings(cls, settings) -> "AuthConfig":
        return cls.from_pem_keys(settings.auth_jwt_issuer, settings.auth_jwt_audience, settings.auth_jwt_public_keys)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _plain_text(value: str, limit: int) -> bool:
    return 0 < len(value) <= limit and value == value.strip() and value.isprintable()


def verify_access_token(token, config: AuthConfig) -> VerifiedClaims:
    """Verify ``token`` against ``config`` or raise AuthenticationError. Never logs or echoes the token."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise AuthenticationError("malformed_token")
    try:
        header = jwt.get_unverified_header(token)  # used only to choose the algorithm check and the key
    except jwt.PyJWTError:
        raise AuthenticationError("malformed_token") from None
    if not isinstance(header, dict):
        raise AuthenticationError("malformed_token")
    if header.get("alg") != ALGORITHM:
        raise AuthenticationError("unsupported_algorithm")
    if header.get("typ") != TOKEN_TYPE:
        raise AuthenticationError("invalid_type")
    if FORBIDDEN_HEADERS & set(header):
        raise AuthenticationError("unsupported_header")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or kid not in config.keys:
        raise AuthenticationError("unknown_key")

    try:
        claims = jwt.decode(
            token,
            config.keys[kid],
            algorithms=[ALGORITHM],  # pinned: the header's algorithm is never trusted
            issuer=config.issuer,
            audience=config.audience,
            leeway=LEEWAY_SECONDS,
            options={"require": list(REQUIRED_CLAIMS), "verify_signature": True},
        )
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("expired") from None
    except jwt.PyJWTError:
        raise AuthenticationError("invalid_token") from None
    except Exception:  # noqa: BLE001 - e.g. a crypto backend error; still a rejection, never a detail
        raise AuthenticationError("invalid_token") from None
    return _validate_claims(claims, config)


def _validate_claims(claims, config: AuthConfig) -> VerifiedClaims:
    if not isinstance(claims, dict):
        raise AuthenticationError("invalid_claims")
    if claims.get("iss") != config.issuer or claims.get("aud") != config.audience:  # aud: one exact string
        raise AuthenticationError("invalid_claims")
    if claims.get("token_use") != TOKEN_USE:
        raise AuthenticationError("invalid_token_use")
    if claims.get("email_verified") is not True:
        raise AuthenticationError("email_not_verified")
    provider, subject, email, jti = claims.get("idp"), claims.get("sub"), claims.get("email"), claims.get("jti")
    iat, exp = claims.get("iat"), claims.get("exp")
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        raise AuthenticationError("unsupported_provider")
    if not isinstance(subject, str) or not _plain_text(subject, MAX_SUBJECT_CHARS):
        raise AuthenticationError("invalid_subject")
    if not isinstance(email, str) or not _plain_text(email, MAX_EMAIL_CHARS) or "@" not in email:
        raise AuthenticationError("invalid_email")
    if not isinstance(jti, str) or not _plain_text(jti, MAX_JTI_CHARS):
        raise AuthenticationError("invalid_jti")
    if not _is_int(iat) or not _is_int(exp) or exp <= iat or exp - iat > MAX_LIFETIME_SECONDS:
        raise AuthenticationError("invalid_lifetime")
    return VerifiedClaims(provider=provider, subject=subject, email=email, jti=jti, issued_at=iat, expires_at=exp)
