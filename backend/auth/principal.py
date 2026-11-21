"""The trusted backend identity of a request."""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, as established by a VERIFIED access token plus database resolution.

    It is built only by ``auth.provisioning.resolve_principal``; nothing in a request body, query
    string, header or WebSocket frame can set or override any field. ``user_id`` is the application
    identity. ``provider``/``provider_subject`` name the external account it was resolved from.
    ``email`` is the verified email ATTRIBUTE observed in the token, never an identity key.
    """

    user_id: uuid.UUID
    provider: str
    provider_subject: str
    email: str
    is_active: bool = True
