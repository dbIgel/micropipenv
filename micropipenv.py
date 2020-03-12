#!/usr/bin/env python3
# micropipenv
# Copyright(C) 2020 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""A lightweight wrapper for pip to support Pipenv files.

This wrapper can convert Pipfile/Pipfile.lock to requirements.in and/or
requirements.txt file suitable for setup.py script or for pip-tools.

Moreover, this wrapper can mimic `pipenv install --deploy`. For any resolved
stack, micropipenv can parse Pipfile/Pipfile.lock and install required
dependencies using raw pip. The virtual environment is not created, but one can
issue `python3 -m venv venv/ && . venv/bin/activate` to create one.
"""

__version__ = "0.1.3"
__author__ = "Fridolin Pokorny <fridex.devel@gmail.com>"
__title__ = "micropipenv"
__all__ = [
    "ArgumentsError",
    "ExtrasMissing",
    "FileNotFound",
    "FileReadError",
    "get_requirements_sections",
    "HashMismatch",
    "install",
    "install_pipenv",
    "install_requirements",
    "install_poetry",
    "main",
    "MicropipenvException",
    "PythonVersionMismatch",
    "requirements",
]

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections import deque
from itertools import chain
from urllib.parse import urlparse

from pip._vendor.packaging.requirements import Requirement

try:
    from pip._internal.req import parse_requirements
except ImportError:  # for pip<10
    from pip.req import parse_requirements

try:
    try:
        from pip._internal.network.session import PipSession
    except ImportError:
        from pip._internal.download import PipSession
except ImportError:
    from pip.download import PipSession
try:
    from pip._internal.index.package_finder import PackageFinder
except ImportError:
    try:
        from pip._internal.index import PackageFinder
    except ImportError:
        from pip.index import PackageFinder

try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any
    from typing import Dict
    from typing import List
    from typing import MutableMapping
    from typing import Optional
    from typing import Tuple
    from typing import Union


_LOGGER = logging.getLogger(__title__)
_DEFAULT_INDEX_URLS = ("https://pypi.org/simple",)
_MAX_DIR_TRAVERSAL = 42  # Avoid any symlinks that would loop.
_PIP_BIN = os.getenv("MICROPIPENV_PIP_BIN", "pip")
_DEBUG = int(os.getenv("MICROPIPENV_DEBUG", 0))
_NO_LOCKFILE_PRINT = int(os.getenv("MICROPIPENV_NO_LOCKFILE_PRINT", 0))
_NO_LOCKFILE_WRITE = int(os.getenv("MICROPIPENV_NO_LOCKFILE_WRITE", 0))


class MicropipenvException(Exception):
    """A base class for all micropipenv exceptions."""


class FileNotFound(MicropipenvException):
    """Raised if the given file was not found on the filesystem."""


class FileReadError(MicropipenvException):
    """Raised if the given file cannot be loaded or parsed."""


class ExtrasMissing(MicropipenvException):
    """Raised when micropipenv was invoked with functionality requiring a missing extras."""


class ArgumentsError(MicropipenvException):
    """Raised when arguments passed are disjoint or wrong."""


class PythonVersionMismatch(MicropipenvException):
    """Raised if Python version found does not correspond to the one present in Pipfile.lock."""


class HashMismatch(MicropipenvException):
    """Raised when computed hash out of Pipfile does not correspond to the hash stated in Pipfile.lock."""


class PipInstallError(MicropipenvException):
    """Raised when `pip install` returned a non-zero exit code."""


class PipRequirementsNotLocked(MicropipenvException):
    """Raised when requirements in requirements.txt are not fully locked."""


class RequirementsError(MicropipenvException):
    """Raised when requirements file has any issue."""


class PoetryError(MicropipenvException):
    """Raised when any of the pyproject.toml or poetry.lock file has any issue."""


def _traverse_up_find_file(file_name):  # type: (str) -> str
    """Traverse the root up, find the given file by name and return its path."""
    path = os.getcwd()
    traversed = _MAX_DIR_TRAVERSAL
    while traversed > 0:
        if file_name in os.listdir(path):
            _LOGGER.debug("Found %r in %r", file_name, path)
            return os.path.join(path, file_name)

        traversed -= 1
        path = os.path.realpath(os.path.join(path, ".."))
    else:
        raise FileNotFound("File {!r} not found in {!r} or any parent directory".format(file_name, os.getcwd()))


def _read_pipfile_lock():  # type: () -> Any
    """Find and load Pipfile.lock."""
    pipfile_lock_path = _traverse_up_find_file("Pipfile.lock")
    try:
        with open(pipfile_lock_path) as input_file:
            content = json.load(input_file)
    except json.JSONDecodeError as exc:
        raise FileReadError("Filed to parse Pipfile.lock: {}".format(str(exc))) from exc
    except Exception as exc:
        raise FileReadError(str(exc)) from exc

    pipfile_spec_version = content.get("_meta", {}).get("pipfile-spec")
    if pipfile_spec_version != 6:
        _LOGGER.warning("Unsupported Pipfile.lock spec version - supported is 6, got {}".format(pipfile_spec_version))

    return content


def _read_pipfile():  # type: () -> Any
    """Find and read Pipfile."""
    try:
        import toml
    except ImportError as exc:
        raise ExtrasMissing(
            "Failed to import toml needed for parsing Pipfile, please install micropipenv "
            "with toml extra: pip install micropipenv[toml]"
        ) from exc

    pipfile_path = _traverse_up_find_file("Pipfile")

    try:
        with open(pipfile_path) as input_file:
            return toml.load(input_file)
    except toml.TomlDecodeError as exc:
        raise FileReadError("Failed to parse Pipfile: {}".format(str(exc))) from exc
    except Exception as exc:
        raise FileReadError(str(exc)) from exc


def _read_poetry():  # type: () -> Tuple[MutableMapping[str, Any], MutableMapping[str, Any]]
    """Find and read poetry.lock and pyproject.toml."""
    try:
        import toml
    except ImportError as exc:
        raise ExtrasMissing(
            "Failed to import toml needed for parsing poetry.lock and pyproject.toml as used by Poetry, "
            "please install micropipenv with toml extra: pip install micropipenv[toml]"
        ) from exc

    poetry_lock_path = _traverse_up_find_file("poetry.lock")
    pyproject_toml_path = _traverse_up_find_file("pyproject.toml")

    try:
        with open(poetry_lock_path) as input_file:
            poetry_lock = toml.load(input_file)
    except toml.TomlDecodeError as exc:
        raise FileReadError("Failed to parse poetry.lock: {}".format(str(exc))) from exc
    except Exception as exc:
        raise FileReadError(str(exc)) from exc

    try:
        with open(pyproject_toml_path) as input_file:
            pyproject_toml = toml.load(input_file)
    except toml.TomlDecodeError as exc:
        raise FileReadError("Failed to parse pyproject.toml: {}".format(str(exc))) from exc
    except Exception as exc:
        raise FileReadError(str(exc)) from exc

    return poetry_lock, pyproject_toml


def _compute_pipfile_hash(pipfile):  # type: (Dict[str, Any]) -> str
    """Compute Pipfile hash based on its content."""
    data = {
        "_meta": {"requires": pipfile.get("requires", {}), "sources": pipfile["source"]},
        "default": pipfile.get("packages", {}),
        "develop": pipfile.get("dev-packages", {}),
    }
    content = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf8")).hexdigest()


def install_pipenv(
    pip_bin=_PIP_BIN, pipfile=None, pipfile_lock=None, *, deploy=False, dev=False, pip_args=None
):  # type: (str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool, bool, Optional[List[str]]) -> None
    """Perform installation of packages from Pipfile.lock."""
    pipfile_lock = pipfile_lock or _read_pipfile_lock()
    _maybe_print_pipfile_lock(pipfile_lock)

    sections = get_requirements_sections(pipfile_lock=pipfile_lock, no_dev=not dev)
    if deploy:
        python_version = pipfile_lock["_meta"].get("requires", {}).get("python_version")
        if python_version is not None:
            if python_version != "{}.{}".format(sys.version_info.major, sys.version_info.minor):
                raise PythonVersionMismatch(
                    "Running Python version {}.{}, but Pipfile.lock requires "
                    "Python version {}".format(sys.version_info.major, sys.version_info.minor, python_version)
                )
        else:
            _LOGGER.warning("No Python version requirement in Pipfile.lock found, no Python version check is performed")

        pipfile_lock_hash = pipfile_lock.get("_meta", {}).get("hash", {}).get("sha256")
        pipfile_hash = _compute_pipfile_hash(pipfile or _read_pipfile())
        if pipfile_hash != pipfile_lock_hash:
            raise HashMismatch(
                "Pipfile.lock hash {!r} does not correspond to hash computed based on "
                "Pipfile {!r}, aborting deployment".format(pipfile_lock_hash, pipfile_hash)
            )

    index_config_str = _get_index_entry_str(sections)

    tmp_file = tempfile.NamedTemporaryFile("w", prefix="requirements.txt", delete=False)
    _LOGGER.debug("Using temporary file for storing requirements: %r", tmp_file.name)

    cmd = [pip_bin, "install", "--no-deps", "--disable-pip-version-check", "-r", tmp_file.name, *(pip_args or [])]
    _LOGGER.debug("Requirements will be installed using %r", cmd)

    packages = chain(sections.get("default", {}).items(), sections.get("develop", {}).items() if dev else [],)

    # We maintain an integer assigned to each package - this integer holds a value - how
    # many times the given package failed to install. If a package fails to install, it is
    # re-scheduled to the next installation round, until we try all the packages to
    # satisfy requirements. If any package succeeds with installation, the integer is
    # set to 0 again for all failed installations. This way we can break "cycles" in
    # installation errors.
    to_install = deque({"package_name": i[0], "info": i[1], "error": 0} for i in packages)
    try:
        while to_install:
            entry = to_install.popleft()
            package_name, info, had_error = entry["package_name"], entry["info"], entry["error"]

            with open(tmp_file.name, "w") as f:
                f.write(index_config_str)
                package_entry_str = _get_package_entry_str(package_name, info)
                f.write(package_entry_str)

            if "git" in info:
                _LOGGER.warning("!!! Requirement %s uses a VCS version: %r", package_name, info)

            _LOGGER.info("Installing %r", package_entry_str)
            called_process = subprocess.run(cmd)

            if called_process.returncode != 0:
                if len(to_install) == 0 or (had_error and had_error > len(to_install)):
                    raise PipInstallError(
                        "Failed to install requirements, dependency {!r} could not be installed".format(package_name)
                    )

                _LOGGER.warning("Failed to install %r, will try in next installation round...", package_name)
                to_install.append({"package_name": package_name, "info": info, "error": had_error + 1})
            else:
                # Discard any error flag if we have any packages that fail to install.
                for item in reversed(to_install):
                    if item["error"] == 0:
                        # Fast path - errors are always added to the end of the queue,
                        # if we find a package without any error we can safely break.
                        break
                    item["error"] = 0
    finally:
        os.remove(tmp_file.name)


def _instantiate_package_finder(pip_session):  # type: (PipSession) -> PackageFinder
    """Instantiate package finder, in a pip>=10 and pip<10 compatible way."""
    try:
        return PackageFinder(find_links=[], session=pip_session, index_urls=_DEFAULT_INDEX_URLS)
    except TypeError:  # API changed in pip>=10
        from pip._internal.models.search_scope import SearchScope
        from pip._internal.models.selection_prefs import SelectionPreferences

        try:
            from pip._internal.index.collector import LinkCollector
        except ModuleNotFoundError:
            from pip._internal.collector import LinkCollector

        link_collector = LinkCollector(session=pip_session, search_scope=SearchScope([], []),)
        selection_prefs = SelectionPreferences(allow_yanked=True,)
        return PackageFinder.create(link_collector=link_collector, selection_prefs=selection_prefs,)


def _requirements2pipfile_lock():  # type: () -> Dict[str, Any]
    """Parse requirements.txt file and return its Pipfile.lock representation."""
    requirements_txt_path = _traverse_up_find_file("requirements.txt")

    pip_session = PipSession()
    finder = _instantiate_package_finder(pip_session)

    result = {}  # type: Dict[str, Any]
    for requirement in parse_requirements(filename=requirements_txt_path, session=PipSession(), finder=finder):
        version = str(requirement.specifier)

        if requirement.specifier is None or not (
            requirement.has_hash_options and len(requirement.specifier) == 1 and version.startswith("==")
        ):
            # Not pinned down software stack using pip-tools.
            raise PipRequirementsNotLocked

        # We add all dependencies to default, develop should not be present in requirements.txt file, but rather
        # in dev-requirements.txt or similar.
        if requirement.name in result:
            raise RequirementsError("Duplicate entry for requirement {}".format(requirement.name))

        if hasattr(requirement, "options"):
            hash_options = requirement.options.get("hashes")
        else:
            hash_options = requirement.hash_options  # More recent pip.

        hashes = []
        for hash_type, hashes_present in hash_options.items():
            hashes.extend(["{}:{}".format(hash_type, h) for h in hashes_present])

        entry = {
            "hashes": sorted(hashes),
            "version": version,
        }

        if requirement.extras:
            entry["extras"] = sorted(requirement.extras)

        if requirement.markers:
            entry["markers"] = str(requirement.markers)

        result[requirement.name] = entry

    sources = []  # type: List[Dict[str, Any]]
    for index_url in chain(finder.index_urls, _DEFAULT_INDEX_URLS):
        if any(s["url"] == index_url for s in sources):
            continue

        sources.append(
            {"name": hashlib.sha256(index_url.encode()).hexdigest(), "url": index_url, "verify_ssl": True}
        )

    if len(sources) == 1:
        # Explicitly assign index if there is just one.
        for entry in result.values():
            entry["index"] = sources[0]["name"]

    with open(requirements_txt_path, "r") as requirements_file:
        requirements_hash = hashlib.sha256(requirements_file.read().encode()).hexdigest()

    return {
        "_meta": {
            "hash": {"sha256": requirements_hash},
            "pipfile-spec": 6,
            "sources": sources,
            "requires": {"python_version": "{}.{}".format(sys.version_info.major, sys.version_info.minor)},
        },
        "default": result,
        "develop": {},
    }


def _maybe_print_pipfile_lock(pipfile_lock):  # type: (Dict[str, Any]) -> None
    """Print and store Pipfile.lock based on configuration supplied."""
    if _NO_LOCKFILE_PRINT and _NO_LOCKFILE_WRITE:
        return

    pipfile_lock_json = json.dumps(pipfile_lock, sort_keys=True, indent=4)

    if not _NO_LOCKFILE_PRINT:
        print("-" * 33 + "- Pipfile.lock -" + "-" * 33, file=sys.stderr)
        print(pipfile_lock_json, file=sys.stderr)
        print("-" * 33 + "- Pipfile.lock -" + "-" * 33, file=sys.stderr)

    if not _NO_LOCKFILE_WRITE:
        try:
            with open("Pipfile.lock", "w") as lock_file:
                lock_file.write(pipfile_lock_json)
        except Exception as exc:
            _LOGGER.warning("Failed to write lockfile to container image: %s", str(exc))


def _maybe_print_pip_freeze(pip_bin):  # type: (str) -> None
    """Print and store requirements.txt based on configuration supplied."""
    if _NO_LOCKFILE_PRINT:
        return

    print("-" * 33 + "- pip freeze -" + "-" * 33, file=sys.stderr)
    cmd = [pip_bin, "freeze", "--disable-pip-version-check"]
    called_process = subprocess.run(cmd)
    print("-" * 33 + "- pip freeze -" + "-" * 33, file=sys.stderr)
    if called_process.returncode != 0:
        _LOGGER.warning("Failed to perform pip freeze to check installed dependencies, the error is not fatal")


def _translate_poetry_dependency(info):  # type: (str) -> str
    """Translate Poetry dependency specification as written in pyproject.toml into its Pipfile.lock equivalent."""
    if isinstance(info, str) and re.match(r"^\d", info):
        return "=={}".format(info)

    # TODO: poetry uses version like ^0.10.4 that are not Pipfile.lock complaint.
    return info


def _poetry2pipfile_lock(
    only_direct=False, no_default=False, no_dev=False
):  # type: (bool, bool, bool) -> Dict[str, Any]
    """Convert Poetry files to Pipfile.lock as Pipenv would produce."""
    poetry_lock, pyproject_toml = _read_poetry()

    pyproject_poetry_section = pyproject_toml.get("tool", {}).get("poetry", {})

    sources = []
    has_default = False  # If default flag is set, it disallows PyPI.
    for item in pyproject_poetry_section.get("source", []):
        sources.append(
            {"name": item["name"], "url": item["url"], "verify_ssl": True}
        )

        has_default = has_default or item.get("default", False)

    for index_url in reversed(_DEFAULT_INDEX_URLS) if not has_default else []:
        # Place defaults as first.
        entry = {
            "url": index_url,
            "name": hashlib.sha256(index_url.encode()).hexdigest(),
            "verify_ssl": True,
        }  # type: Any
        sources.insert(0, entry)

    default = {}
    develop = {}

    if only_direct:
        if not no_default:
            for dependency_name, info in pyproject_poetry_section.get("dependencies", {}).items():
                default[dependency_name] = _translate_poetry_dependency(info)

        if not no_dev:
            for dependency_name, info in pyproject_poetry_section.get("dev-dependencies", {}).items():
                develop[dependency_name] = _translate_poetry_dependency(info)

        return {
            "_meta": {
                "hash": {"sha256": poetry_lock["metadata"]["content-hash"]},
                "pipfile-spec": 6,
                "sources": sources,
                "requires": {"python_version": "{}.{}".format(sys.version_info.major, sys.version_info.minor)},
            },
            "default": default,
            "develop": develop,
        }

    for entry in poetry_lock["package"]:
        hashes = []
        for file_entry in poetry_lock["metadata"]["files"][entry["name"]]:
            hashes.append(file_entry["hash"])

        requirement = {"version": "=={}".format(entry["version"])}  # type: Any

        if hashes:
            requirement["hashes"] = hashes

        if entry.get("marker"):
            requirement["markers"] = entry["marker"]

        if "source" in entry:
            if entry["source"]["type"] != "git":
                raise NotImplementedError(
                    "Micropipenv supports Git VCS, got {} instead".format(entry["source"]["type"])
                )

            requirement["git"] = entry["source"]["url"]
            requirement["ref"] = entry["source"]["reference"]

        # Poetry does not store information about extras in poetry.lock
        # (directly).  It stores extras used for direct dependencies in
        # pyproject.toml but it lacks this info being tracked for the
        # transitive ones. We approximate extras used - we check what
        # dependencies are installed as optional. If they form a set with all
        # dependencies present in specific extra, the given extra was used.
        extra_dependencies = set()

        for dependency_name, dependency_info in entry.get("dependencies", {}).items():
            if isinstance(dependency_info, dict) and dependency_info.get("optional", False):
                extra_dependencies.add(dependency_name)

        for extra_name, extras_listed in entry.get("extras", {}).items():
            # Turn requirement specification into the actual requirement name.
            all_extra_dependencies = set(Requirement(r.split(" ", maxsplit=1)[0]).name for r in extras_listed)
            if all_extra_dependencies.issubset(extra_dependencies):
                if "extras" not in requirement:
                    requirement["extras"] = []

                requirement["extras"].append(extra_name)

        # Sort extras to have always the same output.
        if "extras" in requirement:
            requirement["extras"] = sorted(requirement["extras"])

        if entry["category"] == "main":
            if not no_default:
                default[entry["name"]] = requirement
        elif entry["category"] == "dev":
            if not no_dev:
                develop[entry["name"]] = requirement
        else:
            raise PoetryError("Unknown category for package {}: {}".format(entry["name"], entry["category"]))

    if len(sources) == 1:
        # Explicitly assign index if there is just one.
        for entry in chain(default.values(), develop.values()):
            if "git" not in entry:
                entry["index"] = sources[0]["name"]

    return {
        "_meta": {
            "hash": {"sha256": poetry_lock["metadata"]["content-hash"]},
            "pipfile-spec": 6,
            "sources": sources,
            "requires": {"python_version": "{}.{}".format(sys.version_info.major, sys.version_info.minor)},
        },
        "default": default,
        "develop": develop,
    }


def install_poetry(pip_bin=_PIP_BIN, *, dev=False, pip_args=None):  # type: (str, bool, Optional[List[str]]) -> None
    """Install requirements from poetry.lock."""
    try:
        pipfile_lock = _poetry2pipfile_lock()
    except KeyError as exc:
        raise PoetryError("Failed to parse poetry.lock and pyproject.toml: {}".format(str(exc))) from exc
    install_pipenv(pip_bin, pipfile_lock=pipfile_lock, pip_args=pip_args, dev=dev, deploy=False)


def install_requirements(pip_bin=_PIP_BIN, *, pip_args=None):  # type: (str, Optional[List[str]]) -> None
    """Install requirements from requirements.txt."""
    requirements_txt_path = _traverse_up_find_file("requirements.txt")

    try:
        pipfile_lock = _requirements2pipfile_lock()
        # Deploy set to false as there is no Pipfile to check against.
        install_pipenv(pip_bin, pipfile_lock=pipfile_lock, pip_args=pip_args, deploy=False)
    except PipRequirementsNotLocked:
        _LOGGER.warning("!" * 80)
        _LOGGER.warning("!!!")
        _LOGGER.warning("!!!\t\tThe provided requirements.txt file is not fully locked")
        _LOGGER.warning("!!!")
        _LOGGER.warning("!" * 80)

        cmd = [pip_bin, "install", "-r", requirements_txt_path, "--disable-pip-version-check", *(pip_args or [])]
        _LOGGER.debug("Requirements will be installed using %r", cmd)
        called_process = subprocess.run(cmd)
        if called_process.returncode != 0:
            raise PipInstallError(
                "Failed to install requirements, it's highly recommended to use a lock file to "
                "to make sure correct dependencies are installed in the correct order"
            )

        _maybe_print_pip_freeze(pip_bin)


def install(
    method=None, *, deploy=False, dev=False, pip_args=None
):  # type: (Optional[str], bool, bool, Optional[List[str]]) -> None
    """Perform installation of requirements based on the method used."""
    if method is None:
        try:
            _traverse_up_find_file("Pipfile.lock")
            method = "pipenv"
        except FileNotFound as exc:
            try:
                _LOGGER.info("Failed to find Pipfile.lock, looking up poetry.lock: %s", str(exc))
                _traverse_up_find_file("poetry.lock")
                method = "poetry"
            except FileNotFound as exc:
                _LOGGER.info("Failed to find Poetry lock file, looking up requirements.txt: %s", str(exc))
                _traverse_up_find_file("requirements.txt")
                method = "requirements"

    if method == "requirements":
        if deploy:
            _LOGGER.debug("Discarding deploy flag when requirements.txt are used")

        if dev:
            _LOGGER.debug("Discarding dev flag when requirements.txt are used")

        install_requirements(pip_args=pip_args)
        return
    elif method == "pipenv":
        install_pipenv(deploy=deploy, dev=dev, pip_args=pip_args)
        return
    elif method == "poetry":
        if deploy:
            _LOGGER.debug("Discarding deploy flag when poetry.lock is used")

        install_poetry(pip_args=pip_args, dev=dev)
        return

    raise MicropipenvException("Unhandled method for installing requirements: {}".format(method))


def _parse_pipfile_dependency_info(pipfile_entry):  # type: (Union[str, Dict[str, Any]]) -> Dict[str, Any]
    """Parse a Pipfile entry for a package and return a compatible version with Pipfile.lock entry."""
    if isinstance(pipfile_entry, str):
        return {"version": pipfile_entry}
    elif isinstance(pipfile_entry, dict):
        result = {"version": pipfile_entry["version"]}

        if pipfile_entry.get("extras"):
            result["extras"] = pipfile_entry["extras"]

        if pipfile_entry.get("markers"):
            result["markers"] = pipfile_entry["markers"]

        return result

    raise ValueError("Unknown entry in Pipfile (should be of type dict or a str): {}".format(pipfile_entry))


def get_requirements_sections(
    *, pipfile=None, pipfile_lock=None, no_indexes=False, only_direct=False, no_default=False, no_dev=False
):  # type: (Optional[Dict[str, Any]], Optional[Dict[str, Any]], bool, bool, bool, bool) -> Dict[str, Any]
    """Compute requirements of an application, the output generated is compatible with pip-tools."""
    if no_dev and no_default:
        raise ArgumentsError("Cannot produce requirements as both, default and dev were asked to be discarded")

    result = {
        "default": {},
        "develop": {},
        "sources": [],
    }  # type: Dict[str, Any]

    if only_direct:
        pipfile = pipfile or _read_pipfile()

        if not no_indexes:
            result["sources"] = pipfile.get("source", [])

        if not no_default:
            result["default"] = {
                dependency_name: _parse_pipfile_dependency_info(pipfile_entry)
                for dependency_name, pipfile_entry in pipfile.get("packages", {}).items()
            }

        if not no_dev:
            result["develop"] = {
                dependency_name: _parse_pipfile_dependency_info(pipfile_entry)
                for dependency_name, pipfile_entry in pipfile.get("dev-packages", {}).items()
            }

        return result

    pipfile_lock = pipfile_lock or _read_pipfile_lock()

    if not no_indexes:
        result["sources"] = pipfile_lock.get("_meta", {}).get("sources", [])

    if not no_default:
        result["default"] = pipfile_lock.get("default", {})

    if not no_dev:
        result["develop"] = pipfile_lock.get("develop", {})

    return result


def _get_package_entry_str(
    package_name, info, *, no_hashes=False, no_versions=False
):  # type: (str, Dict[str, Any], bool, bool) -> str
    """Print entry for the given package."""
    if "git" in info:  # A special case for a VCS package
        result = "git+{}".format(info["git"])
        if "ref" in info:
            result += "@{}".format(info["ref"])
        result += "#egg={}".format(package_name)
        return result

    result = package_name
    if info.get("extras"):
        result += "[{}]".format(",".join(info["extras"]))

    if not no_versions and info.get("version") and info["version"] != "*":
        result += info["version"]

    if info.get("markers"):
        result += "; {}".format(info["markers"])

    if not (no_hashes or no_versions):
        for digest in info.get("hashes", []):
            result += " \\\n"
            result += "    --hash={}".format(digest)

    return result + "\n"


def _get_index_entry_str(sections):  # type: (Dict[str, Any]) -> str
    """Get configuration entry for Python package indexes."""
    result = ""
    for idx, source in enumerate(sections.get("sources", [])):
        if idx == 0:
            result += "--index-url {}\n".format(source["url"])
        else:
            result += "--extra-index-url {}\n".format(source["url"])

        if not source["verify_ssl"]:
            result += "--trusted-host {}\n".format(urlparse(source["url"]).netloc)

    return result


def requirements_str(
    sections=None,
    *,
    no_hashes=False,
    no_indexes=False,
    no_versions=False,
    only_direct=False,
    no_default=False,
    no_dev=False,
    no_comments=False,
):  # type: (Optional[Dict[str, Any]], bool, bool, bool, bool, bool, bool, bool) -> str
    """Show requirements of an application, the output generated is compatible with pip-tools."""
    sections = sections or get_requirements_sections(
        no_indexes=no_indexes, only_direct=only_direct, no_default=no_default, no_dev=no_dev
    )

    result = _get_index_entry_str(sections)

    if not no_comments and sections.get("default"):
        result += "#\n# Default dependencies\n#\n"

    for package_name, info in sections.get("default", {}).items():
        result += _get_package_entry_str(package_name, info, no_versions=no_versions, no_hashes=no_hashes)

    if not no_comments and sections.get("develop"):
        result += "#\n# Dev dependencies\n#\n"

    for package_name, info in sections.get("develop", {}).items():
        result += _get_package_entry_str(package_name, info, no_versions=no_versions, no_hashes=no_hashes)

    return result


def requirements(
    method="pipenv",
    sections=None,
    *,
    no_hashes=False,
    no_indexes=False,
    no_versions=False,
    only_direct=False,
    no_default=False,
    no_dev=False,
    no_comments=False,
):  # type: (str, Optional[Dict[str, Any]], bool, bool, bool, bool, bool, bool, bool) -> None
    """Show requirements of an application, the output generated is compatible with pip-tools."""
    if method == "pipenv":
        sections = sections or get_requirements_sections(
            no_indexes=no_indexes, only_direct=only_direct, no_default=no_default, no_dev=no_dev
        )
    elif method == "poetry":
        sections = _poetry2pipfile_lock(only_direct=only_direct, no_default=no_default, no_dev=no_dev)
    else:
        raise MicropipenvException("Unhandled method for installing requirements: {}".format(method))

    print(
        requirements_str(
            sections,
            no_hashes=no_hashes,
            no_indexes=no_indexes,
            no_versions=no_versions,
            only_direct=only_direct,
            no_default=no_default,
            no_dev=no_dev,
            no_comments=no_comments,
        ),
        end="",
    )


def main(argv=None):  # type: (Optional[List[str]]) -> int
    """Main for micropipenv."""
    argv = argv or sys.argv[1:]

    parser = argparse.ArgumentParser(prog=__title__, description=__doc__)
    parser.add_argument("--version", help="Print version information and exit.", action="version", version=__version__)
    parser.add_argument("--verbose", action="count", help="Increase verbosity, can be supplied multiple times.")

    subparsers = parser.add_subparsers()

    parser_install = subparsers.add_parser("install", help=install.__doc__)
    parser_install.add_argument(
        "--deploy",
        help="Abort if the Pipfile.lock is out-of-date, or Python version "
        "is wrong; this requires 'toml' extras to be installed.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_DEPLOY", 0))),
    )
    parser_install.add_argument(
        "--dev",
        help="Install both develop and default packages.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_DEV", os.getenv("PIPENV_DEV", 0)))),
    )
    parser_install.add_argument(
        "--method",
        help="Source of packages for the installation, perform detection if not provided.",
        choices=["pipenv", "requirements", "poetry"],
        default=os.getenv("MICROPIPENV_METHOD")
    )
    parser_install.add_argument(
        "pip_args",
        help="Specify additional argument to pip, can be supplied multiple times (add "
        "'--' to command line to delimit positional arguments that start with dash "
        "from CLI options).",
        nargs="*",
    )
    parser_install.set_defaults(func=install)

    parser_requirements = subparsers.add_parser("requirements", aliases=["req"], help=requirements.__doc__)
    parser_requirements.add_argument(
        "--no-hashes",
        help="Do not include hashes in the generated output.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_NO_HASHES", 0))),
    )
    parser_requirements.add_argument(
        "--no-indexes",
        help="Do not include index configuration in the generated output.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_NO_INDEXES", 0))),
    )
    parser_requirements.add_argument(
        "--no-versions",
        help="Do not include version information in the generated output, implies --no-hashes.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_NO_VERSIONS", 0))),
    )
    parser_requirements.add_argument(
        "--only-direct",
        help="Include only direct dependencies in the output, implies --no-hashes "
        "and --no-versions; this requires 'toml' extras to be installed.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_ONLY_DIRECT", 0))),
    )
    parser_requirements.add_argument(
        "--no-comments",
        help="Do not include comments differentiating sections.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_NO_COMMENTS", 0))),
    )
    parser_requirements.add_argument(
        "--no-default",
        help="Include only development dependencies, do not include default dependencies.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_NO_DEFAULT", 0))),
    )
    parser_requirements.add_argument(
        "--no-dev",
        help="Include only default dependencies, do not include develop dependencies.",
        action="store_true",
        required=False,
        default=bool(int(os.getenv("MICROPIPENV_NO_DEV", 0))),
    )
    parser_requirements.add_argument(
        "--method",
        help="Source of packages for the requirements file, perform detection if not provided.",
        choices=["pipenv", "poetry"],
        default=os.getenv("MICROPIPENV_METHOD", "pipenv")
    )
    parser_requirements.set_defaults(func=requirements)

    arguments = vars(parser.parse_args(argv))
    handler = arguments.pop("func", None)

    if not handler:
        parser.print_help()
        return 1

    verbose = arguments.pop("verbose", 0)
    if (verbose is not None and verbose >= 2) or _DEBUG:
        _LOGGER.setLevel(logging.DEBUG)
        _LOGGER.debug("Debug mode is on.")
        _LOGGER.debug("Running %r with arguments %r", handler.__name__, arguments)
    elif verbose == 1:
        _LOGGER.setLevel(logging.INFO)
    else:
        _LOGGER.setLevel(logging.WARNING)

    try:
        handler(**arguments)
    except Exception as exc:
        if _LOGGER.level <= logging.DEBUG:
            raise

        _LOGGER.error(str(exc))
        return 3

    return 0


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s %(name)-12s %(levelname)-6s %(message)s", datefmt="%m-%d-%y %H:%M:%S")
    sys.exit(main())
