"""M1-D: ES256 access-token verification (generated ephemeral keys; no database, network or real secrets)."""

import logging
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from auth import dependencies, provisioning, tokens  # module objects, imported once at collection
from auth.errors import AuthConfigurationError, AuthenticationError
from auth.tokens import (
    LEEWAY_SECONDS,
    AuthConfig,
    MAX_LIFETIME_SECONDS,
    REQUIRED_CLAIMS,
    VerifiedClaims,
    verify_access_token,
)
from auth_support import AUDIENCE, ISSUER, auth_config, make_token, new_keypair, settings_with

PRIVATE, PUBLIC = new_keypair()
CONFIG = auth_config(PUBLIC)


def reject(token, config=CONFIG) -> str:
    with pytest.raises(AuthenticationError) as exc:
        verify_access_token(token, config)
    # No chained PyJWT/crypto exception (and so none of its text) can reach a caller or a log record.
    assert exc.value.__cause__ is None and (exc.value.__context__ is None or exc.value.__suppress_context__)
    assert str(exc.value) == exc.value.reason  # only a fixed reason code
    return exc.value.reason


def token(**kw):
    return make_token(PRIVATE, **kw)


# ----------------------------------------------------------------------- 1. valid token


def test_valid_token_is_accepted_and_yields_exactly_the_verified_claims():
    now = int(time.time())
    result = verify_access_token(token(now=now), CONFIG)
    assert result == VerifiedClaims(provider="google", subject="google-subject-1", email="user@example.com",
                                    jti="jti-0001", issued_at=now, expires_at=now + 600)


def test_provider_and_subject_are_returned_exactly_as_signed_without_normalization():
    for subject in ("AbC-123", "abc-123", "ünï-sub-ect", "0123456789012345678901"):
        assert verify_access_token(token(claims={"sub": subject}), CONFIG).subject == subject  # no case folding


# --------------------------------------------------- 2-5. signature, kid, algorithm


def test_wrong_signature_is_rejected_even_when_the_claims_are_also_invalid():
    other, _ = new_keypair()
    assert reject(make_token(other)) == "invalid_token"
    # Claims are untrusted until the signature verifies: a forged token with a bad claim reports the signature.
    assert reject(make_token(other, claims={"token_use": "refresh"})) == "invalid_token"


def test_tampered_payload_is_rejected():
    head, body, signature = token().split(".")
    forged = make_token(PRIVATE, claims={"sub": "someone-else"}).split(".")[1]
    assert reject(f"{head}.{forged}.{signature}") == "invalid_token"


@pytest.mark.parametrize("header, drop", [({"kid": "unknown"}, ()), ({"kid": ""}, ()), ({}, ("kid",))])
def test_unknown_or_missing_kid_is_rejected(header, drop):
    assert reject(token(header=header, drop_header=drop)) == "unknown_key"


@pytest.mark.parametrize("kid", [7, ["k1"], {"k": "k1"}, True])
def test_a_non_string_kid_is_rejected(kid):
    assert reject(token(header={"kid": kid})) in ("malformed_token", "unknown_key")  # PyJWT rejects it while parsing


def test_alg_none_is_rejected():
    assert reject(token(header={"alg": "none"}, signer="none")) == "unsupported_algorithm"
    assert reject(token(header={"alg": "None"}, signer="none")) == "unsupported_algorithm"


def test_hs256_signed_with_the_public_key_is_rejected():
    assert reject(token(header={"alg": "HS256"}, signer="hs256", public_pem=PUBLIC)) == "unsupported_algorithm"


@pytest.mark.parametrize("alg", ["RS256", "ES384", "ES512", "EdDSA", "PS256", "es256", ""])
def test_any_algorithm_but_es256_is_rejected(alg):
    assert reject(token(header={"alg": alg})) == "unsupported_algorithm"


def test_a_token_that_claims_es256_but_is_signed_by_another_algorithm_is_rejected():
    assert reject(token(signer="hs256", public_pem=PUBLIC)) == "invalid_token"  # header says ES256, signature is HMAC
    assert reject(token(signer="none")) == "invalid_token"


# ------------------------------------------------------- 6-9. issuer, audience, time


def test_wrong_issuer_is_rejected():
    assert reject(token(claims={"iss": "someone-else"})) == "invalid_token"
    assert reject(token(claims={"iss": ISSUER + " "})) == "invalid_token"


def test_wrong_audience_is_rejected_and_the_audience_must_be_one_exact_string():
    assert reject(token(claims={"aud": "another-api"})) == "invalid_token"
    assert reject(token(claims={"aud": [AUDIENCE, "other"]})) == "invalid_claims"  # a list, even containing ours
    assert reject(token(claims={"aud": [AUDIENCE]})) == "invalid_claims"


def test_expired_token_outside_the_30_second_leeway_is_rejected():
    now = int(time.time())
    assert LEEWAY_SECONDS == 30
    assert reject(token(claims={"iat": now - 631, "exp": now - 31})) == "expired"
    assert reject(token(claims={"iat": now - 700, "exp": now - 100})) == "expired"


def test_tokens_inside_the_30_second_leeway_are_accepted():
    now = int(time.time())
    assert verify_access_token(token(claims={"iat": now - 629, "exp": now - 29}), CONFIG)  # expired 29 s ago
    assert verify_access_token(token(claims={"iat": now + 29, "exp": now + 629}), CONFIG)  # issued 29 s in the future


def test_token_issued_in_the_future_beyond_the_leeway_is_rejected():
    now = int(time.time())
    assert reject(token(claims={"iat": now + 31, "exp": now + 631})) == "invalid_token"


def test_the_lifetime_is_at_most_ten_minutes_and_positive():
    now = int(time.time())
    assert MAX_LIFETIME_SECONDS == 600
    assert verify_access_token(token(claims={"iat": now, "exp": now + 600}), CONFIG)
    assert reject(token(claims={"iat": now, "exp": now + 601})) == "invalid_lifetime"
    assert reject(token(claims={"iat": now, "exp": now + 3600})) == "invalid_lifetime"
    assert reject(token(claims={"iat": now, "exp": now})) == "invalid_lifetime"
    assert reject(token(claims={"iat": now + 5, "exp": now + 2})) == "invalid_lifetime"


# ------------------------------------------------------------- 10. required claims


@pytest.mark.parametrize("claim", list(REQUIRED_CLAIMS))
def test_every_required_claim_is_required(claim):
    assert reject(token(drop_claims=(claim,))) == "invalid_token"  # PyJWT reports the missing required claim


def test_the_required_claims_are_exactly_the_approved_contract():
    assert set(REQUIRED_CLAIMS) == {"sub", "idp", "email", "email_verified", "jti", "iat", "exp", "token_use", "iss", "aud"}


# ----------------------------------------------------- 11-14. typ, token_use, email, provider


@pytest.mark.parametrize("header, drop", [({"typ": "at+jwt"}, ()), ({"typ": "jwt"}, ()), ({"typ": "JWS"}, ()),
                                          ({"typ": None}, ()), ({"typ": ["JWT"]}, ()), ({}, ("typ",))])
def test_wrong_or_missing_typ_is_rejected(header, drop):
    assert reject(token(header=header, drop_header=drop)) == "invalid_type"


@pytest.mark.parametrize("value", ["refresh", "ACCESS", "access ", "", ["access"], 1])
def test_wrong_token_use_is_rejected(value):
    assert reject(token(claims={"token_use": value})) == "invalid_token_use"


@pytest.mark.parametrize("value", [False, "true", "True", 1, 0, [True], {"v": True}])
def test_unverified_or_malformed_email_verified_is_rejected(value):
    assert reject(token(claims={"email_verified": value})) == "email_not_verified"


@pytest.mark.parametrize("value", ["github", "Google", "GOOGLE", " google", "google ", "", "unknown", ["google"], 5])
def test_unsupported_provider_is_rejected(value):
    assert reject(token(claims={"idp": value})) == "unsupported_provider"


# ------------------------------------------------------------ 15. malformed claim types


@pytest.mark.parametrize("name, claims_override", [
    ("exp as string", {"exp": "9999999999"}),
    ("exp as float", {"exp": time.time() + 300.5}),
    ("exp as bool", {"exp": True}),
    ("iat as string", {"iat": "1700000000"}),
    ("iat as list", {"iat": [1]}),
    ("sub as int", {"sub": 12345}),
    ("sub empty", {"sub": ""}),
    ("sub whitespace only", {"sub": "   "}),
    ("sub leading space", {"sub": " subject"}),
    ("sub trailing space", {"sub": "subject "}),
    ("sub too long", {"sub": "s" * 256}),
    ("sub control character", {"sub": "sub\nject"}),
    ("sub null byte", {"sub": "sub\x00ject"}),
    ("email as int", {"email": 5}),
    ("email empty", {"email": ""}),
    ("email without @", {"email": "not-an-email"}),
    ("email with edge space", {"email": " user@example.com"}),
    ("email too long", {"email": "u" * 320 + "@example.com"}),
    ("jti as int", {"jti": 7}),
    ("jti empty", {"jti": ""}),
    ("jti too long", {"jti": "j" * 129}),
    ("idp as list", {"idp": ["google"]}),
    ("token_use as list", {"token_use": ["access"]}),
])
def test_malformed_claim_types_are_rejected(name, claims_override):
    reject(token(claims=claims_override))


def test_the_subject_may_be_at_the_exact_column_limit():
    assert verify_access_token(token(claims={"sub": "s" * 255}), CONFIG).subject == "s" * 255


# ------------------------------------------------- structural and header hardening


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.b.c.d", "....", None, 123, b"bytes", ["x"], "x" * 4097])
def test_malformed_tokens_are_rejected(bad):
    assert reject(bad) == "malformed_token"


@pytest.mark.parametrize("extra", [{"jku": "https://evil.example/keys"}, {"jwk": {"kty": "EC"}},
                                   {"x5u": "https://evil.example/c"}, {"x5c": ["MIIB"]}])
def test_key_reference_headers_are_rejected(extra):
    assert reject(token(header=extra)) == "unsupported_header"


def test_a_critical_headers_token_is_rejected():
    assert reject(token(header={"crit": ["exp"]})) in ("unsupported_header", "malformed_token")  # PyJWT refuses crit itself


@pytest.mark.parametrize("name", ["token_use", "email_verified", "idp", "sub", "email", "jti", "iat", "exp", "iss", "aud"])
def test_a_null_claim_counts_as_missing(name):
    assert reject(token(claims={name: None})) == "invalid_token"


def test_key_rotation_two_keys_by_kid_and_a_kid_cannot_vouch_for_another_keys_signature():
    other_private, other_public = new_keypair()
    config = auth_config(PUBLIC, kid="k1", k2=other_public)
    assert verify_access_token(make_token(other_private, header={"kid": "k2"}), config)
    assert verify_access_token(make_token(PRIVATE, header={"kid": "k1"}), config)
    assert reject(make_token(other_private, header={"kid": "k1"}), config) == "invalid_token"
    assert reject(make_token(PRIVATE, header={"kid": "k2"}), config) == "invalid_token"


def test_no_rejection_leaks_the_token_a_key_or_claim_text_in_exceptions_or_logs(caplog):
    secret_subject = "SENTINEL-SUBJECT-must-not-leak"
    bad_tokens = [token(claims={"sub": secret_subject, "token_use": "nope"}), make_token(new_keypair()[0], claims={"sub": secret_subject})]
    with caplog.at_level(logging.DEBUG):
        for bad in bad_tokens:
            with pytest.raises(AuthenticationError) as exc:
                verify_access_token(bad, CONFIG)
            blob = f"{exc.value} {exc.value!r} {exc.value.args}"
            assert bad not in blob and secret_subject not in blob and "BEGIN PUBLIC KEY" not in blob
    assert secret_subject not in caplog.text and "BEGIN" not in caplog.text
    assert all(bad not in caplog.text for bad in bad_tokens)


# ------------------------------------------------------ 16. configuration fails closed


def test_config_from_settings_uses_the_configured_issuer_audience_and_keys():
    import json

    _, pem = new_keypair()
    config = AuthConfig.from_settings(settings_with(json.dumps({"kid-a": PUBLIC, "kid-b": pem})))
    assert config.issuer == "agentforge-web" and config.audience == "agentforge-api"
    assert set(config.keys) == {"kid-a", "kid-b"}


def test_config_fails_closed_when_keys_are_missing_or_unusable():
    import json

    # AuthConfig / AuthConfigurationError are imported at the top of this module on purpose: other tests purge
    # and re-import backend modules, and a runtime import here would raise a different exception class.
    _, p384 = new_keypair(ec.SECP384R1())
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    garbage = "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----\n"  # structurally a PEM, cryptographically junk
    private_pem = PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
    cases = {
        "no keys configured": ({}, "no_public_keys"),
        "garbage PEM": ({"k": garbage}, "unparseable_public_key"),
        "not a PEM at all": ({"k": "hello"}, "unparseable_public_key"),
        "RSA key": ({"k": rsa_pem}, "public_key_not_p256"),
        "P-384 key": ({"k": p384}, "public_key_not_p256"),
        "a private key": ({"k": private_pem}, "unparseable_public_key"),
        "non-string value": ({"k": 5}, "invalid_key_entry"),
        "blank kid": ({" ": PUBLIC}, "invalid_key_entry"),
    }
    for label, (keys, reason) in cases.items():
        with pytest.raises(AuthConfigurationError) as exc:
            AuthConfig.from_pem_keys(ISSUER, AUDIENCE, keys)
        assert exc.value.reason == reason, label
        assert "BEGIN" not in f"{exc.value} {exc.value!r}" and exc.value.__cause__ is None, label
    for issuer, audience in (("", AUDIENCE), (" ", AUDIENCE), (ISSUER, ""), (ISSUER, "  "), (None, AUDIENCE)):
        with pytest.raises(AuthConfigurationError) as exc:
            AuthConfig.from_pem_keys(issuer, audience, {"k": PUBLIC})
        assert exc.value.reason == "issuer_or_audience_missing"
    # From settings: no configured keys, and a blank issuer on an otherwise valid settings object.
    with pytest.raises(AuthConfigurationError):
        AuthConfig.from_settings(settings_with())
    with pytest.raises(AuthConfigurationError):
        AuthConfig.from_settings(settings_with(json.dumps({"k": PUBLIC}), auth_jwt_issuer=" "))


def test_the_backend_never_handles_a_private_key():
    for module in (tokens, provisioning, dependencies):
        source = open(module.__file__, encoding="utf-8").read()
        assert "private_key" not in source and "PrivateFormat" not in source
        assert ".sign(" not in source and "jwt.encode" not in source  # the backend can verify, never mint
