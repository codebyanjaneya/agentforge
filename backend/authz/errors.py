"""Authorization failures. Each carries only a fixed reason CODE, never an email, id, claim or driver text."""


class AuthzError(Exception):
    reason: str

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AuthorizationError(AuthzError):
    """The Principal is not allowed (not a member, unknown workspace, not an owner, inconsistent workspace...)."""


class ScopeError(AuthzError):
    """A memory operation was attempted without a valid, authorized ``MemoryScope``. Always fails closed."""
