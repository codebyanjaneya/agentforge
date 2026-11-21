"""Backend authentication (M1-D): ES256 access-token verification, identity resolution, Principal.

Modules (no circular imports; each only imports the ones listed):
* ``errors``       - the three failure types and their fixed reason codes.
* ``principal``    - the immutable ``Principal``.
* ``tokens``       - verifies a JWT against an ``AuthConfig`` (PyJWT + cryptography); imports ``errors``.
* ``provisioning`` - resolves ``(provider, subject)`` to an internal user, provisioning on first use;
                     imports ``errors``, ``principal``, ``tokens`` (types) and ``memory.models``.
* ``dependencies`` - the FastAPI dependency ``get_principal``; imports all of the above and ``config``.

Nothing here is wired into any route yet (M1-F). The backend holds public keys only: signing belongs to the
Next.js issuer (M1-G).
"""
