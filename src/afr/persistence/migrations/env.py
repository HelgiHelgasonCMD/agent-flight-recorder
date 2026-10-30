from __future__ import annotations

from alembic import context

from afr.persistence.tables import metadata

config = context.config
connection = config.attributes.get("connection")
if connection is None:  # pragma: no cover - only used from afr.persistence.db
    raise RuntimeError("afr migrations must be run through afr.persistence.db.migrate()")

context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
