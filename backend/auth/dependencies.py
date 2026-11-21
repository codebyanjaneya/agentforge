"""FastAPI wiring: ``get_principal`` turns ``Authorization: Bearer <access token>`` into a Principal.

Behaviour (nothing is attached to any route yet; M1-F wires routes):
* no / malformed bearer header, or any token that fails verification -> 401 ``Not authenticated`` with
  ``WWW-Authenticate: Bearer``;
* a valid token for an inactive user -> 403 ``Forbidden`` (never reactivated);
* missing or invalid server auth configuration, or any unexpected internal failure -> 500
  ``Authentication is not available``: it fails closed and there is NO bypass or development shortcut;
* bodies are fixed strings: no key, token, claim, PyJWT, cryptography, SQLAlchemy or driver text. Logs carry only
  fixed reason codes and exception class names, never the token, a key or a claim.

The Principal is derived only from the verified token plus database resolution; request JSON, query
parameters, other headers and WebSocket frames are never consulted.
"""

import logging

from fastapi import Depends, HTTPException, Request

from auth.errors import AuthConfigurationError, AuthenticationError, InactiveUserError
from auth.principal import Principal
from auth.provisioning import resolve_principal
from auth.tokens import AuthConfig, verify_access_token
from config import get_settings

logger = logging.getLogger("agentforge.auth")

_UNAUTHENTICATED = {"status_code": 401, "detail": "Not authenticated", "headers": {"WWW-Authenticate": "Bearer"}}
_cache: dict = {}


def reset_auth_config_cache() -> None:
    _cache.clear()


def get_auth_config() -> AuthConfig:
    """The verifier configuration from the application settings; fails closed when it is not usable."""
    try:
        settings = get_settings()
        cache_key = (settings.auth_jwt_issuer, settings.auth_jwt_audience, tuple(sorted(settings.auth_jwt_public_keys.items())))
        if cache_key not in _cache:
            _cache.clear()
            _cache[cache_key] = AuthConfig.from_settings(settings)
        return _cache[cache_key]
    except AuthConfigurationError as exc:
        logger.error("Authentication is misconfigured (%s)", exc.reason)
    except Exception as exc:  # noqa: BLE001 - e.g. settings unavailable; never expose the cause
        logger.error("Authentication configuration is unavailable (%s)", type(exc).__name__)
    raise HTTPException(status_code=500, detail="Authentication is not available")


def get_session_factory():
    """The application's session factory (imported lazily: importing it builds the engine)."""
    from memory.long_term import SessionLocal

    return SessionLocal


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header:
        raise HTTPException(**_UNAUTHENTICATED)
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or token != token.strip() or " " in token:
        raise HTTPException(**_UNAUTHENTICATED)
    return token


def get_principal(
    request: Request,
    config: AuthConfig = Depends(get_auth_config),
    session_factory=Depends(get_session_factory),
) -> Principal:
    token = _bearer_token(request)
    try:
        claims = verify_access_token(token, config)
        return resolve_principal(session_factory, claims)
    except AuthenticationError as exc:
        logger.info("Authentication rejected (%s)", exc.reason)
        raise HTTPException(**_UNAUTHENTICATED) from None
    except InactiveUserError as exc:
        logger.info("Authentication refused (%s)", exc.reason)
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except AuthConfigurationError as exc:
        logger.error("Authentication is misconfigured (%s)", exc.reason)
        raise HTTPException(status_code=500, detail="Authentication is not available") from None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - database or internal failure: fail closed, expose nothing
        logger.error("Authentication failed unexpectedly (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Authentication is not available") from None
