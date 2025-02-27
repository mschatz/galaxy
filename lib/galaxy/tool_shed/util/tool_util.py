import logging
import os
import shutil

import galaxy.tools
from galaxy import util
from galaxy.datatypes.sniff import is_column_based
from galaxy.tool_shed.util import basic_util
from galaxy.util import checkers
from galaxy.util.expressions import ExpressionContext
from galaxy.web.form_builder import SelectField

log = logging.getLogger(__name__)


def build_shed_tool_conf_select_field(app):
    """Build a SelectField whose options are the keys in app.toolbox.shed_tool_confs."""
    options = []
    for dynamic_tool_conf_filename in app.toolbox.dynamic_conf_filenames():
        if dynamic_tool_conf_filename.startswith("./"):
            option_label = dynamic_tool_conf_filename.replace("./", "", 1)
        else:
            option_label = dynamic_tool_conf_filename
        options.append((option_label, dynamic_tool_conf_filename))
    select_field = SelectField(name="shed_tool_conf")
    for option_tup in options:
        select_field.add_option(option_tup[0], option_tup[1])
    return select_field


def build_tool_panel_section_select_field(app):
    """Build a SelectField whose options are the sections of the current in-memory toolbox."""
    options = []
    for section_id, section_name in app.toolbox.get_sections():
        options.append((section_name, section_id))
    select_field = SelectField(name="tool_panel_section_id", field_id="tool_panel_section_select")
    for option_tup in options:
        select_field.add_option(option_tup[0], option_tup[1])
    return select_field


def copy_sample_file(app, filename, dest_path=None):
    """
    Copies a sample file at `filename` to `the dest_path`
    directory and strips the '.sample' extensions from `filename`.
    Returns the path to the copied file (with the .sample extension).
    """
    if dest_path is None:
        dest_path = os.path.abspath(app.config.tool_data_path)
    sample_file_name = basic_util.strip_path(filename)
    copied_file = sample_file_name.rsplit(".sample", 1)[0]
    full_source_path = os.path.abspath(filename)
    full_destination_path = os.path.join(dest_path, sample_file_name)
    # Don't copy a file to itself - not sure how this happens, but sometimes it does...
    if full_source_path != full_destination_path:
        # It's ok to overwrite the .sample version of the file.
        shutil.copy(full_source_path, full_destination_path)
    # Only create the .loc file if it does not yet exist.  We don't overwrite it in case it
    # contains stuff proprietary to the local instance.
    non_sample_path = os.path.join(dest_path, copied_file)
    if not os.path.lexists(non_sample_path):
        shutil.copy(full_source_path, os.path.join(dest_path, copied_file))
    return non_sample_path


def copy_sample_files(app, sample_files, tool_path=None, sample_files_copied=None, dest_path=None):
    """
    Copy all appropriate files to dest_path in the local Galaxy environment that have not
    already been copied.  Those that have been copied are contained in sample_files_copied.
    The default value for dest_path is ~/tool-data.  We need to be careful to copy only
    appropriate files here because tool shed repositories can contain files ending in .sample
    that should not be copied to the ~/tool-data directory.
    """
    filenames_not_to_copy = ["tool_data_table_conf.xml.sample"]
    sample_files_copied = util.listify(sample_files_copied)
    for filename in sample_files:
        filename_sans_path = os.path.split(filename)[1]
        if filename_sans_path not in filenames_not_to_copy and filename not in sample_files_copied:
            if tool_path:
                filename = os.path.join(tool_path, filename)
            # Attempt to ensure we're copying an appropriate file.
            if is_data_index_sample_file(filename):
                copy_sample_file(app, filename, dest_path=dest_path)


def generate_message_for_invalid_tools(
    app, invalid_file_tups, repository, metadata_dict, as_html=True, displaying_invalid_tool=False
):
    if as_html:
        new_line = "<br/>"
        bold_start = "<b>"
        bold_end = "</b>"
    else:
        new_line = "\n"
        bold_start = ""
        bold_end = ""
    message = ""
    if app.name == "galaxy":
        tip_rev = str(repository.changeset_revision)
    else:
        tip_rev = str(repository.tip())
    if not displaying_invalid_tool:
        if metadata_dict:
            message += f"Metadata may have been defined for some items in revision '{tip_rev}'.  "
            message += f"Correct the following problems if necessary and reset metadata.{new_line}"
        else:
            message += f"Metadata cannot be defined for revision '{tip_rev}' so this revision cannot be automatically "
            message += (
                f"installed into a local Galaxy instance.  Correct the following problems and reset metadata.{new_line}"
            )
    for itc_tup in invalid_file_tups:
        tool_file, exception_msg = itc_tup
        if exception_msg.find("No such file or directory") >= 0:
            exception_items = exception_msg.split()
            missing_file_items = exception_items[7].split("/")
            missing_file = missing_file_items[-1].rstrip("'")
            if missing_file.endswith(".loc"):
                sample_ext = f"{missing_file}.sample"
            else:
                sample_ext = missing_file
            correction_msg = "This file refers to a missing file %s%s%s.  " % (bold_start, str(missing_file), bold_end)
            correction_msg += "Upload a file named %s%s%s to the repository to correct this error." % (
                bold_start,
                sample_ext,
                bold_end,
            )
        else:
            if as_html:
                correction_msg = exception_msg
            else:
                correction_msg = (
                    exception_msg.replace("<br/>", new_line).replace("<b>", bold_start).replace("</b>", bold_end)
                )
        message += f"{bold_start}{tool_file}{bold_end} - {correction_msg}{new_line}"
    return message


def get_tool_path_install_dir(partial_install_dir, shed_tool_conf_dict, tool_dict, config_elems):
    for elem in config_elems:
        if elem.tag == "tool":
            if elem.get("guid") == tool_dict["guid"]:
                tool_path = shed_tool_conf_dict["tool_path"]
                relative_install_dir = os.path.join(tool_path, partial_install_dir)
                return tool_path, relative_install_dir
        elif elem.tag == "section":
            for section_elem in elem:
                if section_elem.tag == "tool":
                    if section_elem.get("guid") == tool_dict["guid"]:
                        tool_path = shed_tool_conf_dict["tool_path"]
                        relative_install_dir = os.path.join(tool_path, partial_install_dir)
                        return tool_path, relative_install_dir
    return None, None


def handle_missing_index_file(app, tool_path, sample_files, repository_tools_tups, sample_files_copied):
    """
    Inspect each tool to see if it has any input parameters that are dynamically
    generated select lists that depend on a .loc file.  This method is not called
    from the tool shed, but from Galaxy when a repository is being installed.
    """
    for repository_tools_tup in repository_tools_tups:
        tup_path, guid, repository_tool = repository_tools_tup
        params_with_missing_index_file = repository_tool.params_with_missing_index_file
        for param in params_with_missing_index_file:
            options = param.options
            missing_file_name = basic_util.strip_path(options.missing_index_file)
            if missing_file_name not in sample_files_copied:
                # The repository must contain the required xxx.loc.sample file.
                for sample_file in sample_files:
                    sample_file_name = basic_util.strip_path(sample_file)
                    if sample_file_name == f"{missing_file_name}.sample":
                        target_path = copy_sample_file(app, os.path.join(tool_path, sample_file))
                        if options.tool_data_table and options.tool_data_table.missing_index_file:
                            options.tool_data_table.handle_found_index_file(target_path)
                        sample_files_copied.append(target_path)
                        break
    return repository_tools_tups, sample_files_copied


def is_data_index_sample_file(file_path):
    """
    Attempt to determine if a .sample file is appropriate for copying to ~/tool-data when
    a tool shed repository is being installed into a Galaxy instance.
    """
    # Currently most data index files are tabular, so check that first.  We'll assume that
    # if the file is tabular, it's ok to copy.
    if is_column_based(file_path):
        return True
    # If the file is any of the following, don't copy it.
    if checkers.check_html(file_path):
        return False
    if checkers.check_image(file_path):
        return False
    if checkers.check_binary(name=file_path):
        return False
    if checkers.is_bz2(file_path):
        return False
    if checkers.is_gzip(file_path):
        return False
    if checkers.is_zip(file_path):
        return False
    # Default to copying the file if none of the above are true.
    return True


def new_state(trans, tool, invalid=False):
    """Create a new `DefaultToolState` for the received tool.  Only inputs on the first page will be initialized."""
    state = galaxy.tools.DefaultToolState()
    state.inputs = {}
    if invalid:
        # We're attempting to display a tool in the tool shed that has been determined to have errors, so is invalid.
        return state
    try:
        # Attempt to generate the tool state using the standard Galaxy-side code
        return tool.new_state(trans)
    except Exception as e:
        # Fall back to building tool state as below
        log.debug(
            'Failed to build tool state for tool "%s" using standard method, will try to fall back on custom method: %s',
            tool.id,
            e,
        )
    inputs = tool.inputs_by_page[0]
    context = ExpressionContext(state.inputs, parent=None)
    for input in inputs.values():
        try:
            state.inputs[input.name] = input.get_initial_value(trans, context)
        except Exception:
            # FIXME: not all values should be an empty list
            state.inputs[input.name] = []
    return state


def panel_entry_per_tool(tool_section_dict):
    # Return True if tool_section_dict looks like this.
    # {<Tool guid> :
    #    [{ tool_config : <tool_config_file>,
    #       id: <ToolSection id>,
    #       version : <ToolSection version>,
    #       name : <TooSection name>}]}
    # But not like this.
    # { id: <ToolSection id>, version : <ToolSection version>, name : <TooSection name>}
    if not tool_section_dict:
        return False
    if len(tool_section_dict) != 3:
        return True
    for k in tool_section_dict.keys():
        if k not in ["id", "version", "name"]:
            return True
    return False
