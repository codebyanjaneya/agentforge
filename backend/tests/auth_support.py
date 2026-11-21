"""Test support for the M1-D authentication tests: ephemeral ES256 keys, an independent token builder and an
isolated migrated SQLite database. No real key, token, database or network is used.

Tokens are assembled by hand (header and payload JSON, ``cryptography`` ECDSA, raw r||s signature) instead of
PyJWT's encoder, so the verifier is tested against tokens produced independently of the library under test,
including deliberately invalid ones (unsigned ``alg: none``, HS256 keyed with the public PEM, missing headers).
"""

import base64
import hashlib
import hmac
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from auth.tokens import AuthConfig, VerifiedClaims
from migration_support import Db

ISSUER = "agentforge-web"
AUDIENCE = "agentforge-api"


def b64u(data) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_keypair(curve=None):
    private = ec.generate_private_key(curve or ec.SECP256R1())
    pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, pem


def sign_es256(private_key, signing_input: bytes) -> bytes:
    r, s = decode_dss_signature(private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")  # IEEE P1363, as JWT requires


def make_token(private_key, claims=None, header=None, drop_claims=(), drop_header=(), signer="es256", public_pem=None, now=None):
    """Build a compact JWT. ``claims``/``header`` override the valid defaults; ``signer`` is es256, none or hs256."""
    now = int(now if now is not None else time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "google-subject-1", "idp": "google", "email": "user@example.com",
               "email_verified": True, "token_use": "access", "jti": "jti-0001", "iat": now, "exp": now + 600}
    payload.update(claims or {})
    for name in drop_claims:
        payload.pop(name)
    head = {"alg": "ES256", "typ": "JWT", "kid": "k1"}
    head.update(header or {})
    for name in drop_header:
        head.pop(name)
    signing_input = f"{b64u(json.dumps(head, separators=(',', ':')))}.{b64u(json.dumps(payload, separators=(',', ':')))}".encode()
    if signer == "es256":
        signature = sign_es256(private_key, signing_input)
    elif signer == "none":
        signature = b""
    elif signer == "hs256":
        signature = hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest()
    else:  # pragma: no cover
        raise ValueError(signer)
    return signing_input.decode() + "." + b64u(signature)


def auth_config(public_pem: str, kid: str = "k1", **extra_keys) -> AuthConfig:
    return AuthConfig.from_pem_keys(ISSUER, AUDIENCE, {kid: public_pem, **extra_keys})


def settings_with(keys_json=None, **overrides):
    """Application ``Settings`` for tests (development environment); ``overrides`` replace fields afterwards."""
    from config import load_settings

    env = {"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:pw@db.internal/app", "APP_ENV": "development"}
    if keys_json is not None:
        env["AUTH_JWT_PUBLIC_KEYS"] = keys_json
    settings = load_settings(env)
    return settings.model_copy(update=overrides) if overrides else settings


def claims(provider="google", subject="subject-1", email="user@example.com", jti="jti-1") -> VerifiedClaims:
    now = int(time.time())
    return VerifiedClaims(provider=provider, subject=subject, email=email, jti=jti, issued_at=now, expires_at=now + 600)


class AuthDb:
    """A throw-away SQLite file migrated to head through the real env.py, with foreign keys enforced."""

    def __init__(self, tmp_path, monkeypatch):
        self.db = Db(tmp_path / "auth.db", monkeypatch)
        self.db.upgrade()
        self.engine = create_engine(self.db.url, connect_args={"timeout": 30, "check_same_thread": False})
        event.listen(self.engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def scalar(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def rows(self, sql: str, **params):
        with self.engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]

    def run(self, sql: str, **params):
        with self.engine.begin() as conn:
            conn.execute(text(sql), params)

    def counts(self) -> dict:
        return {
            "users": self.scalar("SELECT count(*) FROM users"),
            "workspaces": self.scalar("SELECT count(*) FROM workspaces"),
            "memberships": self.scalar("SELECT count(*) FROM workspace_memberships"),
            "identities": self.scalar("SELECT count(*) FROM auth_identities"),
        }


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    database = AuthDb(tmp_path, monkeypatch)
    yield database
    database.engine.dispose()


ZERO = {"users": 0, "workspaces": 0, "memberships": 0, "identities": 0}
ONE_OF_EACH = {"users": 1, "workspaces": 1, "memberships": 1, "identities": 1}
