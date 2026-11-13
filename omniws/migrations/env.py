from alembic import context
from sqlalchemy import Connection

from omniws import models  # noqa: F401  (registers the tables)
from omniws.db import Base

target_metadata = Base.metadata


def run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run(connection)
else:  # `uv run alembic ...` from the command line uses the configured data directory
    from omniws.config import get_settings
    from omniws.db import make_engine

    with make_engine(get_settings().omni_data_dir).connect() as connection:
        run(connection)
