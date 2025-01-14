import logging
import os.path
import sys

from migrate.versioning import (
    repository,
    schema,
)
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
)
from sqlalchemy.exc import NoSuchTableError

from galaxy.model.database_utils import (
    create_database,
    database_exists,
)
from galaxy.model.tool_shed_install import mapping

log = logging.getLogger(__name__)

# path relative to galaxy
migrate_repository_directory = os.path.abspath(os.path.dirname(__file__)).replace(os.getcwd() + os.path.sep, "", 1)
migrate_repository = repository.Repository(migrate_repository_directory)


def create_or_verify_database(url, engine_options=None, app=None):
    """ """
    # Create engine and metadata
    engine_options = engine_options or {}
    if not database_exists(url):
        message = f"Creating database for URI [{url}]"
        log.info(message)
        create_database(url)

    engine = create_engine(url, **engine_options)

    def migrate():
        try:
            # Declare the database to be under a repository's version control
            db_schema = schema.ControlledSchema.create(engine, migrate_repository)
        except Exception:
            # The database is already under version control
            db_schema = schema.ControlledSchema(engine, migrate_repository)
        # Apply all scripts to get to current version
        migrate_to_current_version(engine, db_schema)

    meta = MetaData(bind=engine)
    if app and getattr(app.config, "database_auto_migrate", False):
        migrate()
        return

    # Try to load tool_shed_repository table
    try:
        Table("tool_shed_repository", meta, autoload=True)
    except NoSuchTableError:
        # No table means a completely uninitialized database.  If we
        # have an app, we'll set its new_installation setting to True
        # so the tool migration process will be skipped.
        log.info("Creating install database from scratch, skipping migrations")
        mapping.init(url=url, create_tables=True)
        current_version = migrate_repository.version().version
        schema.ControlledSchema.create(engine, migrate_repository, version=current_version)
        db_schema = schema.ControlledSchema(engine, migrate_repository)
        assert db_schema.version == current_version
        migrate()
        return

    try:
        Table("migrate_version", meta, autoload=True)
    except NoSuchTableError:
        # The database exists but is not yet under migrate version control, so init with version 1
        log.info("Adding version control to existing database")
        try:
            Table("metadata_file", meta, autoload=True)
            schema.ControlledSchema.create(engine, migrate_repository, version=2)
        except NoSuchTableError:
            schema.ControlledSchema.create(engine, migrate_repository, version=1)

    # Verify that the code and the DB are in sync
    db_schema = schema.ControlledSchema(engine, migrate_repository)
    if migrate_repository.versions.latest != db_schema.version:
        exception_msg = "Your database has version '%d' but this code expects version '%d'.  " % (
            db_schema.version,
            migrate_repository.versions.latest,
        )
        exception_msg += "Back up your database and then migrate the schema by running the following from your Galaxy installation directory:"
        exception_msg += "\n\nsh manage_db.sh upgrade install\n"

    else:
        log.info("At database version %d" % db_schema.version)


def migrate_to_current_version(engine, schema):
    # Changes to get to current version
    changeset = schema.changeset(None)
    for ver, change in changeset:
        nextver = ver + changeset.step
        log.info(f"Migrating {ver} -> {nextver}... ")
        old_stdout = sys.stdout

        class FakeStdout:
            def __init__(self):
                self.buffer = []

            def write(self, s):
                self.buffer.append(s)

            def flush(self):
                pass

        sys.stdout = FakeStdout()
        try:
            schema.runchange(ver, change, changeset.step)
        finally:
            for message in "".join(sys.stdout.buffer).split("\n"):
                log.info(message)
            sys.stdout = old_stdout
