"""Alembic environment (generated migrations live in versions/; excluded from coverage, see pyproject)."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from samawi_bot.db.models import Base

config = context.config
target_metadata = Base.metadata

connectable = engine_from_config(config.get_section(config.config_ini_section) or {}, prefix="sqlalchemy.", poolclass=pool.NullPool)
with connectable.connect() as connection:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()
