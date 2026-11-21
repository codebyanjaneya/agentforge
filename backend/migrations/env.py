"""Alembic environment.

Imports only ``memory.models`` (metadata): never ``memory.long_term``, application
settings, LLM clients or an engine at import time. The database URL is never read
from alembic.ini, never stored in Alembic's config, and never printed or logged.
"""

import logging.config

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from memory.models import Base

config = context.config
# Tests (and embedding callers) can set config.attributes["configure_logger"] = False.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Offline (--sql) mode only needs the dialect; this placeholder carries no host or credentials.
OFFLINE_PLACEHOLDER_URL = "postgresql://"
# Serializes concurrent migration runs on PostgreSQL (held until the transaction ends).
ADVISORY_LOCK_KEY = 7_041_522_001


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        if name == "alembic_version":
            return False
        # Tables that exist in the database but not in the metadata are unmanaged:
        # autogenerate must never propose dropping them.
        if reflected and compare_to is None:
            return False
    elif type_ == "column":
        # Same for columns inside managed tables: database-only columns are unmanaged,
        # so autogenerate must never propose DROP COLUMN. Metadata-only columns
        # (reflected=False) stay included so legitimate additions are detected.
        if reflected and compare_to is None:
            return False
    return True


def _configure_kwargs() -> dict:
    return dict(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=OFFLINE_PLACEHOLDER_URL,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from config import get_database_url  # lazy: offline mode needs no settings at all

    url = get_database_url()  # ConfigError names variables, never values
    try:
        engine = create_engine(url.get_secret_value(), poolclass=NullPool)
        connection = engine.connect()
    except Exception as exc:
        # Driver/URL errors can contain host or user text; surface only the class name.
        raise RuntimeError(f"Could not connect to the database ({type(exc).__name__}).") from None
    with connection:
        context.configure(connection=connection, **_configure_kwargs())
        # One transaction: PostgreSQL DDL is transactional, so a failure leaves no partial state.
        with context.begin_transaction():
            if connection.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
