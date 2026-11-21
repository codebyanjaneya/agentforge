"""Domain authorization (M1-E): workspace membership, the owner invariant and ``MemoryScope``.

The trusted identity is the M1-D ``Principal`` (``backend/auth``); nothing here verifies tokens or builds a
Principal. A client-supplied workspace id is only a *requested resource*: it authorizes nothing until
membership is read from the database for the Principal's internal user id.

* ``errors``     - ``AuthorizationError`` and ``ScopeError`` with fixed reason codes.
* ``scope``      - the immutable ``MemoryScope`` (user id + authorized workspace id).
* ``workspaces`` - membership resolution, the owner invariant and the (owner-only) membership operations.
"""
