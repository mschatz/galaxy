import json
import os
import shutil
import tempfile
import traceback
from typing import (
    Callable,
    NamedTuple,
)

from galaxy.datatypes.registry import Registry
from galaxy.files import ConfiguredFileSources
from galaxy.job_execution.compute_environment import SharedComputeEnvironment
from galaxy.job_execution.setup import JobIO
from galaxy.metadata.set_metadata import (
    get_metadata_params,
    get_object_store,
    validate_and_load_datatypes_config,
)
from galaxy.model import store
from galaxy.model.store import SessionlessContext
from galaxy.objectstore import ObjectStore
from galaxy.structured_app import MinimalToolApp
from galaxy.tool_util.parser.factory import get_tool_source
from galaxy.tools import (
    create_tool_from_source,
    evaluation,
)
from galaxy.tools.data import ToolDataTableManager
from galaxy.util.bunch import Bunch
from galaxy.util.dbkeys import GenomeBuilds


class ToolAppConfig(NamedTuple):
    name: str
    tool_data_path: str
    galaxy_data_manager_data_path: str
    nginx_upload_path: str
    len_file_path: str
    builds_file_path: str
    root: str
    is_admin_user: Callable
    admin_users: list = []


class ToolApp(MinimalToolApp):
    """Dummy App that allows loading tools"""

    name = "tool_app"

    def __init__(
        self,
        sa_session: SessionlessContext,
        tool_app_config: ToolAppConfig,
        datatypes_registry: Registry,
        object_store: ObjectStore,
        tool_data_table_manager: ToolDataTableManager,
        file_sources: ConfiguredFileSources,
    ):
        self.model = Bunch(context=sa_session)
        self.config = tool_app_config
        self.datatypes_registry = datatypes_registry
        self.object_store = object_store
        self.genome_builds = GenomeBuilds(self)
        self.tool_data_tables = tool_data_table_manager
        self.file_sources = file_sources
        self.biotools_metadata_source = None


def main(TMPDIR, WORKING_DIRECTORY, IMPORT_STORE_DIRECTORY):
    metadata_params = get_metadata_params(WORKING_DIRECTORY)
    datatypes_config = metadata_params["datatypes_config"]
    if not os.path.exists(datatypes_config):
        datatypes_config = os.path.join(WORKING_DIRECTORY, "configs", datatypes_config)
    datatypes_registry = validate_and_load_datatypes_config(datatypes_config)
    object_store = get_object_store(WORKING_DIRECTORY)
    import_store = store.imported_store_for_metadata(IMPORT_STORE_DIRECTORY)
    # TODO: clean up random places from which we read files in the working directory
    job_io = JobIO.from_json(os.path.join(IMPORT_STORE_DIRECTORY, "job_io.json"), sa_session=import_store.sa_session)
    tool_app_config = ToolAppConfig(
        name="tool_app",
        tool_data_path=job_io.tool_data_path,
        galaxy_data_manager_data_path=job_io.galaxy_data_manager_data_path,
        nginx_upload_path=TMPDIR,
        len_file_path=job_io.len_file_path,
        builds_file_path=job_io.builds_file_path,
        root=TMPDIR,
        is_admin_user=lambda _: job_io.user_context.is_admin,
    )
    with open(os.path.join(IMPORT_STORE_DIRECTORY, "tool_data_tables.json")) as data_tables_json:
        tdtm = ToolDataTableManager.from_dict(json.load(data_tables_json))
    app = ToolApp(
        sa_session=import_store.sa_session,
        tool_app_config=tool_app_config,
        datatypes_registry=datatypes_registry,
        object_store=object_store,
        tool_data_table_manager=tdtm,
        file_sources=job_io.file_sources,
    )
    # TODO: could try to serialize just a minimal tool variant instead of the whole thing ?
    tool_source = get_tool_source(tool_source_class=job_io.tool_source_class, raw_tool_source=job_io.tool_source)
    tool = create_tool_from_source(app, tool_source=tool_source, tool_dir=job_io.tool_dir)
    tool_evaluator = evaluation.RemoteToolEvaluator(
        app=app, tool=tool, job=job_io.job, local_working_directory=WORKING_DIRECTORY
    )
    tool_evaluator.set_compute_environment(compute_environment=SharedComputeEnvironment(job_io=job_io, job=job_io.job))
    with open(os.path.join(WORKING_DIRECTORY, "tool_script.sh"), "a") as out:
        command_line, extra_filenames, environment_variables = tool_evaluator.build()
        out.write(command_line)


if __name__ == "__main__":
    TMPDIR = tempfile.mkdtemp()
    WORKING_DIRECTORY = os.getcwd()
    WORKING_PARENT = os.path.join(WORKING_DIRECTORY, os.path.pardir)
    if not os.path.isdir("working") and os.path.isdir(os.path.join(WORKING_PARENT, "working")):
        # We're probably in pulsar
        WORKING_DIRECTORY = WORKING_PARENT
    METADATA_DIRECTORY = os.path.join(WORKING_DIRECTORY, "metadata")
    IMPORT_STORE_DIRECTORY = os.path.join(METADATA_DIRECTORY, "outputs_new")
    EXPORT_STORE_DIRECTORY = os.path.join(METADATA_DIRECTORY, "outputs_populated")
    try:
        main(TMPDIR, WORKING_DIRECTORY, IMPORT_STORE_DIRECTORY)
    except Exception:
        os.makedirs(EXPORT_STORE_DIRECTORY, exist_ok=True)
        with open(os.path.join(EXPORT_STORE_DIRECTORY, "traceback.txt"), "w") as out:
            out.write(traceback.format_exc())
        raise
    finally:
        shutil.rmtree(TMPDIR, ignore_errors=True)
