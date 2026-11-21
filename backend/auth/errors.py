"""Authentication failures. Each carries only a fixed reason CODE, never token, key, claim or driver text."""


class AuthError(Exception):
    reason: str

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AuthenticationError(AuthError):
    """Missing or invalid credentials: the client gets 401."""


class InactiveUserError(AuthError):
    """A valid token for a deactivated user: the client gets 403. The user is never reactivated."""


class AuthConfigurationError(AuthError):
    """Server-side authentication configuration is missing or invalid: fail closed (500, sanitized)."""
