"""Alembic environment. Runs only through `swarmeval.db.migrate`, which passes the connection."""

from alembic import context
from sqlalchemy import Connection

from swarmeval.db.tables import metadata

connection: Connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, include_schemas=True)
with context.begin_transaction():
    context.run_migrations()
