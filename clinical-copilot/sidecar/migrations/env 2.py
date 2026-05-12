"""Alembic migration environment.

Reads the database URL from ``COPILOT_DATABASE_URL`` (preferred) or the
sidecar settings, never from ``alembic.ini``. Keeping credentials out of
the source tree is the only safe story when the same repo is built into
multiple environments.

Online vs offline:

- ``run_migrations_online`` opens a real connection and runs migrations
  inside a transaction. Used in CI and production.
- ``run_migrations_offline`` emits SQL to stdout for human review. Used
  during code review of a database-touching pull request.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


# ─── Logging ──────────────────────────────────────────────────────────
# Alembic's CLI passes a ConfigParser proxy as ``context.config``. When
# the file has a [loggers] section we honor it; otherwise (e.g. when env.py
# is imported by a programmatic test that did not load alembic.ini) we
# silently skip logging configuration.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_database_url() -> str:
    """Resolve the database URL with a clear precedence order.

    1. ``COPILOT_DATABASE_URL`` environment variable (production override).
    2. ``DATABASE_URL`` environment variable (Heroku/Railway-style).
    3. ``sidecar.config`` settings (local development default).

    Raises a clear ``RuntimeError`` if no URL is available, so a missing
    env var produces a one-line failure rather than a cryptic SQLAlchemy
    error two stack frames deep.
    """
    direct = os.environ.get("COPILOT_DATABASE_URL")
    if direct:
        return direct

    fallback = os.environ.get("DATABASE_URL")
    if fallback:
        return fallback

    try:
        from sidecar.config import get_settings  # noqa: WPS433 — lazy on purpose
        return get_settings().database_url
    except Exception as exc:  # pragma: no cover — defensive
        raise RuntimeError(
            "Alembic cannot resolve a database URL. Set COPILOT_DATABASE_URL "
            "or DATABASE_URL, or ensure sidecar.config.get_settings() returns "
            "a database_url. Underlying error: " + repr(exc)
        ) from exc


def run_migrations_offline() -> None:
    """Render the SQL for the configured target without connecting."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Open a real connection and run migrations inside a transaction."""
    url = _resolve_database_url()
    cfg_section = config.get_section(config.config_ini_section) or {}
    cfg_section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
