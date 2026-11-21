"""M1-D: the FastAPI ``get_principal`` dependency, on a throw-away app (no existing route uses it yet)."""

import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from auth import dependencies, provisioning
from auth.dependencies import get_auth_config, get_principal, get_session_factory, reset_auth_config_cache
from auth.principal import Principal
from auth_support import auth_config, auth_db, make_token, new_keypair, settings_with  # noqa: F401
from config import ConfigError

BACKEND = Path(__file__).resolve().parent.parent
PRIVATE, PUBLIC = new_keypair()
UNAUTHENTICATED = {"detail": "Not authenticated"}
UNAVAILABLE = {"detail": "Authentication is not available"}
FORBIDDEN = {"detail": "Forbidden"}
LEAKY_WORDS = ("BEGIN", "jwt", "PyJWT", "cryptography", "sqlalchemy", "sqlite", "psycopg", "Traceback", "SENTINEL")


@pytest.fixture(autouse=True)
def fresh_config_cache():
    reset_auth_config_cache()
    yield
    reset_auth_config_cache()


def build_client(auth_db, config=None):
    app = FastAPI()

    @app.get("/whoami")
    def whoami(request: Request, principal: Principal = Depends(get_principal)):
        return {"user_id": str(principal.user_id), "email": principal.email, "provider": principal.provider,
                "subject": principal.provider_subject, "query_email": request.query_params.get("user_email")}

    @app.post("/echo")
    def echo(body: dict, principal: Principal = Depends(get_principal)):
        return {"email": principal.email, "body": body}

    if config is not None:
        app.dependency_overrides[get_auth_config] = lambda: config
    app.dependency_overrides[get_session_factory] = lambda: auth_db.session_factory
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(auth_db):
    return build_client(auth_db, auth_config(PUBLIC))


def bearer(token=None, **kw):
    return {"Authorization": f"Bearer {token or make_token(PRIVATE, **kw)}"}


def assert_clean(response, *forbidden):
    text = response.text
    for word in LEAKY_WORDS + tuple(forbidden):
        assert word not in text, (word, text)


# ---------------------------------------------------- missing and malformed credentials


def test_missing_authorization_header_is_401_with_a_bearer_challenge(client):
    r = client.get("/whoami")
    assert r.status_code == 401 and r.json() == UNAUTHENTICATED
    assert r.headers["www-authenticate"] == "Bearer"
    assert_clean(r)


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "bearer", "Token abc.def.ghi", "Bearer  two.spaces.here",
                                    "Bearer a b", "Bearer  ", "abc.def.ghi", "", "Bearer\tabc.def.ghi"])
def test_malformed_authorization_headers_are_401(client, header):
    r = client.get("/whoami", headers={"Authorization": header})
    assert r.status_code == 401 and r.json() == UNAUTHENTICATED
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("fmt", ["Token {t}", "Basic {t}", "Digest {t}", "Bearer  {t}", "Bearer {t} extra", "Bearer {t}  ", "Bearer\t{t}",
                                 "{t}", "Bearer: {t}"])
def test_a_VALID_token_in_the_wrong_header_format_is_still_401(auth_db, client, fmt):
    r = client.get("/whoami", headers={"Authorization": fmt.format(t=make_token(PRIVATE))})
    assert r.status_code == 401 and r.json() == UNAUTHENTICATED
    assert auth_db.counts() == {"users": 0, "workspaces": 0, "memberships": 0, "identities": 0}


def test_the_scheme_is_case_insensitive_and_a_valid_token_authenticates(client):
    r = client.get("/whoami", headers={"Authorization": "bearer " + make_token(PRIVATE)})
    assert r.status_code == 200 and r.json()["provider"] == "google"


# ----------------------------------------------------------------------- invalid tokens


@pytest.mark.parametrize("name, token", [
    ("garbage", "not-a-jwt"),
    ("wrong signature", make_token(new_keypair()[0])),
    ("expired", make_token(PRIVATE, claims={"iat": 1_600_000_000, "exp": 1_600_000_600})),
    ("alg none", make_token(PRIVATE, header={"alg": "none"}, signer="none")),
    ("unverified email", make_token(PRIVATE, claims={"email_verified": False})),
    ("unsupported provider", make_token(PRIVATE, claims={"idp": "github"})),
    ("wrong audience", make_token(PRIVATE, claims={"aud": "other"})),
])
def test_invalid_credentials_are_401_without_detail_and_never_provision_anything(auth_db, client, name, token):
    r = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json() == UNAUTHENTICATED, name
    assert r.headers["www-authenticate"] == "Bearer"
    assert_clean(r, token)
    assert auth_db.counts() == {"users": 0, "workspaces": 0, "memberships": 0, "identities": 0}


# ------------------------------------------------------------------ valid token, 403


def test_a_valid_token_authenticates_and_provisions_once(auth_db, client):
    first = client.get("/whoami", headers=bearer(claims={"sub": "sub-A", "email": "a@example.com"}))
    second = client.get("/whoami", headers=bearer(claims={"sub": "sub-A", "email": "a@example.com", "jti": "other"}))
    assert first.status_code == second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"] and uuid.UUID(first.json()["user_id"])
    assert (first.json()["provider"], first.json()["subject"], first.json()["email"]) == ("google", "sub-A", "a@example.com")
    assert auth_db.counts() == {"users": 1, "workspaces": 1, "memberships": 1, "identities": 1}


def test_an_inactive_user_gets_403_and_is_never_reactivated(auth_db, client):
    assert client.get("/whoami", headers=bearer()).status_code == 200
    auth_db.run("UPDATE users SET is_active = 0")
    r = client.get("/whoami", headers=bearer())
    assert r.status_code == 403 and r.json() == FORBIDDEN
    assert "www-authenticate" not in r.headers
    assert_clean(r)
    assert auth_db.scalar("SELECT is_active FROM users") == 0 and auth_db.counts()["users"] == 1


# --------------------------------------------- client-supplied identity cannot override


def test_client_supplied_email_cannot_override_the_resolved_principal(auth_db, client):
    attacker = "attacker@evil.example"
    token_headers = bearer(claims={"sub": "sub-A", "email": "real@example.com"})
    r = client.get(f"/whoami?user_email={attacker}&email={attacker}", headers={
        **token_headers, "X-User-Email": attacker, "X-Forwarded-Email": attacker, "X-Auth-Request-Email": attacker})
    assert r.status_code == 200
    assert r.json()["email"] == "real@example.com"  # from the verified token, never from the request
    assert r.json()["query_email"] == attacker  # the route can still SEE the parameter; the Principal ignores it
    r = client.post("/echo", json={"user_email": attacker, "email": attacker, "user_id": str(uuid.uuid4())}, headers=token_headers)
    assert r.status_code == 200 and r.json()["email"] == "real@example.com"
    assert auth_db.scalar("SELECT count(*) FROM users WHERE email LIKE :e", e=f"%{attacker}%") == 0
    assert auth_db.scalar("SELECT count(*) FROM auth_identities WHERE email_at_link LIKE :e", e=f"%{attacker}%") == 0
    assert auth_db.counts()["users"] == 1


def test_no_credentials_means_no_identity_even_if_the_client_names_an_email(client):
    r = client.get("/whoami?user_email=anyone@example.com", headers={"X-User-Email": "anyone@example.com"})
    assert r.status_code == 401 and r.json() == UNAUTHENTICATED


# ---------------------------------------------------- configuration fails closed (500)


def real_config_client(auth_db, monkeypatch, settings=None, error=None):
    """A client that uses the REAL get_auth_config, fed by a patched settings accessor."""
    def fake_get_settings():
        if error is not None:
            raise error
        return settings

    monkeypatch.setattr(dependencies, "get_settings", fake_get_settings)
    return build_client(auth_db)  # no config override


def rsa_public_pem():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


@pytest.mark.parametrize("label", ["no keys", "structurally valid but junk PEM", "RSA public key", "settings unavailable", "blank issuer"])
def test_missing_or_invalid_auth_configuration_fails_closed_with_a_sanitized_500(auth_db, monkeypatch, caplog, label):
    junk = "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----\n"
    settings, error = None, None
    if label == "no keys":
        settings = settings_with()
    elif label == "structurally valid but junk PEM":
        settings = settings_with(json.dumps({"k1": junk}))
    elif label == "RSA public key":
        settings = settings_with(json.dumps({"k1": rsa_public_pem()}))
    elif label == "settings unavailable":
        error = ConfigError(["DATABASE_URL: required"])
    else:
        settings = settings_with(json.dumps({"k1": PUBLIC}), auth_jwt_issuer=" ")
    client = real_config_client(auth_db, monkeypatch, settings, error)
    with caplog.at_level(logging.DEBUG):
        valid = client.get("/whoami", headers=bearer())  # even a perfectly valid token cannot get in
        anonymous = client.get("/whoami")  # and the failure is visible regardless of credentials
    for r in (valid, anonymous):
        assert r.status_code == 500 and r.json() == UNAVAILABLE, label
        assert_clean(r, PUBLIC.splitlines()[1])
        assert r.headers.get("www-authenticate") is None
    assert "BEGIN" not in caplog.text and "MFkw" not in caplog.text and "DATABASE_URL" not in caplog.text
    assert auth_db.counts() == {"users": 0, "workspaces": 0, "memberships": 0, "identities": 0}


def test_there_is_no_bypass_when_auth_is_not_flagged_as_required(auth_db, monkeypatch):
    settings = settings_with()  # AUTH_CONFIG_REQUIRED is false and there are no keys
    assert settings.auth_config_required is False and settings.auth_jwt_public_keys == {}
    client = real_config_client(auth_db, monkeypatch, settings)
    assert client.get("/whoami", headers=bearer()).status_code == 500  # still refuses: never silently open
    assert client.get("/whoami").status_code == 500


def test_the_real_settings_path_authenticates_with_the_configured_key_issuer_and_audience(auth_db, monkeypatch):
    client = real_config_client(auth_db, monkeypatch, settings_with(json.dumps({"k1": PUBLIC})))
    r = client.get("/whoami", headers=bearer(claims={"sub": "from-settings"}))
    assert r.status_code == 200 and r.json()["subject"] == "from-settings"
    assert client.get("/whoami", headers=bearer(claims={"iss": "other-issuer"})).status_code == 401


def test_a_corrected_configuration_takes_effect_without_a_restart(auth_db, monkeypatch):
    holder = {"settings": settings_with()}
    monkeypatch.setattr(dependencies, "get_settings", lambda: holder["settings"])
    client = build_client(auth_db)
    assert client.get("/whoami", headers=bearer()).status_code == 500
    holder["settings"] = settings_with(json.dumps({"k1": PUBLIC}))
    assert client.get("/whoami", headers=bearer()).status_code == 200


# --------------------------------------------- internal failures are sanitized and logged safely


def test_database_failures_are_a_sanitized_500_and_the_driver_text_stays_out_of_bodies_and_logs(auth_db, caplog):
    class Broken:
        def __call__(self):
            raise OperationalError("SELECT secret", {"p": "SENTINEL-param"}, Exception("SENTINEL-driver-text postgresql://u:SENTINEL-pw@h/db"))

    client = build_client(auth_db, auth_config(PUBLIC))
    client.app.dependency_overrides[get_session_factory] = lambda: Broken()
    with caplog.at_level(logging.DEBUG):
        r = client.get("/whoami", headers=bearer())
    assert r.status_code == 500 and r.json() == UNAVAILABLE
    assert_clean(r)
    assert "SENTINEL" not in caplog.text and "OperationalError" in caplog.text  # the class name only


def test_an_unrelated_integrity_error_is_a_sanitized_500_and_leaves_nothing_behind(auth_db, client, monkeypatch, caplog):
    monkeypatch.setattr(provisioning, "OWNER_ROLE", "viewer")
    with caplog.at_level(logging.DEBUG):
        r = client.get("/whoami", headers=bearer())
    assert r.status_code == 500 and r.json() == UNAVAILABLE
    assert_clean(r, "ck_workspace_memberships_role")
    assert "ck_workspace_memberships_role" not in caplog.text
    assert auth_db.counts() == {"users": 0, "workspaces": 0, "memberships": 0, "identities": 0}


def test_an_unexpected_verifier_error_is_a_sanitized_500(auth_db, client, monkeypatch):
    monkeypatch.setattr(dependencies, "verify_access_token", lambda *a: (_ for _ in ()).throw(ValueError("SENTINEL-internal")))
    r = client.get("/whoami", headers=bearer())
    assert r.status_code == 500 and r.json() == UNAVAILABLE
    assert_clean(r)


def test_no_log_record_contains_a_token_a_key_or_an_email(auth_db, client, caplog):
    token = make_token(PRIVATE, claims={"sub": "SENTINEL-sub", "email": "private.person@example.com"})
    bad = make_token(new_keypair()[0], claims={"sub": "SENTINEL-sub", "email": "private.person@example.com"})
    auth_db.run("UPDATE users SET is_active = 1")
    with caplog.at_level(logging.DEBUG):
        client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        client.get("/whoami", headers={"Authorization": f"Bearer {bad}"})
        auth_db.run("UPDATE users SET is_active = 0")
        client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    for secret in (token, bad, "SENTINEL-sub", "private.person@example.com", "BEGIN PUBLIC KEY", PUBLIC.splitlines()[1]):
        assert secret not in caplog.text, secret


# --------------------------------------------------------- scope: nothing else was wired


def test_importing_the_auth_package_builds_no_engine_and_pulls_in_no_application_modules():
    code = ("import sys, auth.dependencies, auth.provisioning, auth.tokens, auth.principal, auth.errors;"
            "bad=[m for m in ('memory.long_term','main','telegram_bot','orchestrator','voice_handler','redis','groq') if m in sys.modules];"
            "print(bad); sys.exit(1 if bad else 0)")
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "PYTHON_DOTENV_DISABLED": "1"}
    env.pop("GROQ_API_KEY", None)
    env.pop("DATABASE_URL", None)
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def _main_routes() -> dict:
    """(METHOD, path) -> (handler name, parameter names, names used in Depends(...) defaults), read from main.py's AST."""
    import ast

    tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
    routes = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and isinstance(deco.func.value, ast.Name)
                    and deco.func.value.id == "app" and deco.func.attr in {"get", "post", "put", "patch", "delete", "websocket"}):
                method = "WS" if deco.func.attr == "websocket" else deco.func.attr.upper()
                defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
                deps = {a.id for d in defaults if isinstance(d, ast.Call) and getattr(d.func, "id", None) == "Depends"
                        for a in d.args if isinstance(a, ast.Name)}
                params = {a.arg for a in node.args.args + node.args.kwonlyargs}
                routes[(method, deco.args[0].value)] = (node.name, params, deps)
    return routes


def test_main_routes_use_the_authenticated_principal_and_workspace_scope_and_public_ones_are_explicit(import_fresh):
    import inspect

    from fastapi.dependencies.utils import get_dependant

    from authz.dependencies import get_scope

    routes = _main_routes()
    # the single dependency every user-specific route uses: verified Principal -> workspace authorization
    protected = {("POST", "/autonomous/run"), ("POST", "/tasks/schedule"), ("GET", "/tasks"), ("DELETE", "/tasks/{task_id}"),
                 ("POST", "/improve/{agent_name}"), ("GET", "/improve/{agent_name}"), ("GET", "/history")}
    assert {key for key, (_, _, deps) in routes.items() if "get_scope" in deps} == protected
    for key in protected:
        name, params, deps = routes[key]
        assert "get_scope" in deps and "get_principal" not in deps, (key, deps)  # never a bare get_principal: authorization cannot be skipped
        # the ONLY other direct dependency a protected route may have is the execution service (M2-B), nothing else
        assert deps - {"get_scope"} <= {"get_execution_service"}, (key, deps)
        assert not {"user_email", "email"} & params, key  # no identity parameter in a protected contract
    assert {key for key in protected if "get_execution_service" in routes[key][2]} == {("POST", "/autonomous/run")}  # and only this route has it

    # get_scope really is authentication + the reserved-header check + workspace authorization (checked on the callable)
    def tree(call, seen=()):
        dependant = get_dependant(path="/", call=call)
        found = {dep.call for dep in dependant.dependencies}
        for dep in dependant.dependencies:
            found |= tree(dep.call)
        return found

    assert get_principal in tree(get_scope) and get_session_factory in tree(get_scope)
    source = inspect.getsource(get_scope)
    assert "authorize_memory_scope" in source and "authorize_personal_scope" in source

    # the execution service dependency is inspected on the REAL callable: it only resolves through the session factory, it
    # is neither an authentication path nor a replacement for get_scope (the route still lists get_scope itself)
    main = import_fresh("main")
    graph = tree(main.get_execution_service)
    direct = {dep.call for dep in get_dependant(path="/", call=main.get_execution_service).dependencies}
    assert direct == {get_session_factory} and get_session_factory in graph
    assert get_principal not in graph and get_scope not in graph and get_auth_config not in graph
    assert main.get_scope is get_scope  # the dependency the protected routes use is the M1-F one, unchanged

    # public routes: exactly these, none of them wired through the user-authentication dependencies
    public = {key for key in routes if key not in protected}
    assert public == {("GET", "/health"), ("GET", "/ready"), ("GET", "/metrics"), ("POST", "/voice/incoming"), ("POST", "/voice/process"), ("WS", "/ws")}
    for key in public:
        assert not {"get_scope", "get_principal"} & routes[key][2], key
    main_source = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert routes[("GET", "/health")][2] == set() and 'return {"status": "ok"}' in main_source
    # the Twilio webhooks are explicitly identified as a temporary, NOT secure, service boundary pending S2
    assert "PUBLIC and NOT SECURE until S2" in main_source and "Twilio service webhooks" in main_source
    # the WebSocket authenticates inside its handler (first message), not through a REST dependency
    assert "handle_websocket(" in main_source


def test_telegram_and_voice_do_not_use_the_new_authentication_system_yet():
    bot = (BACKEND / "telegram_bot.py").read_text(encoding="utf-8")
    voice = (BACKEND / "voice_handler.py").read_text(encoding="utf-8")
    assert "auth." not in bot.replace("author", "") and "from auth" not in voice
    assert "authz" not in bot and "authz" not in voice
