"""What `zelos extensions package` and the marketplace read, checked before a tag is pushed."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import zelos_extension_instruments
from zelos_extension_instruments import ACTION_PREFIX, __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((REPO_ROOT / "extension.toml").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_entry_module_re_exports_the_action_prefix() -> None:
    """The inventory dump reads the prefix off the entry module and falls back to its file name,
    so a missing re-export ships `main/list_instruments` beside a live `Instruments/...`."""
    import main

    assert main.ACTION_PREFIX == ACTION_PREFIX == MANIFEST["name"] == "Instruments"


def test_versions_agree() -> None:
    assert __version__ == MANIFEST["version"] == PYPROJECT["project"]["version"]
    assert zelos_extension_instruments.__version__ == __version__


def test_manifest_hosts_an_agent() -> None:
    assert "agent" not in MANIFEST
    assert MANIFEST["host"]["type"] == "agent"
    assert MANIFEST["host"]["agent"]["entry"] == "main.py"


@pytest.mark.parametrize("relative", MANIFEST["package"]["paths"])
def test_package_path_exists(relative: str) -> None:
    assert (REPO_ROOT / relative).exists()


@pytest.mark.parametrize("key", ["icon", "readme"])
def test_referenced_file_exists(key: str) -> None:
    assert (REPO_ROOT / MANIFEST[key]).is_file()


def test_entry_point_is_packaged() -> None:
    assert MANIFEST["host"]["agent"]["entry"] in MANIFEST["package"]["paths"]


def test_config_schema_is_valid_and_its_examples_validate() -> None:
    """Draft 7, because it is the draft that enforces the per-connection `dependencies`."""
    schema = json.loads((REPO_ROOT / MANIFEST["config"]["schema"]).read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    assert schema["examples"]
    for example in schema["examples"]:
        validator.validate(example)


def test_inventory_holds_the_three_hidden_read_only_standalone_actions(tmp_path: Path) -> None:
    """The agent serves this file while the extension is stopped; the config form's pickers and
    Auto-configure need all three actions in it."""
    out = tmp_path / "actions.json"
    subprocess.run(
        [
            *(sys.executable, "-m", "zelos_sdk.extensions.actions", "dump"),
            *("--entry", MANIFEST["host"]["agent"]["entry"], "--out", str(out)),
        ],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
    )

    inventory = json.loads(out.read_text(encoding="utf-8"))
    assert inventory["action_prefix"] == ACTION_PREFIX == "Instruments"
    assert {a["path"]: (a["read_only"], a["hidden"]) for a in inventory["actions"]} == {
        "list_instruments": (True, True),
        "list_profiles": (True, True),
        "auto_config": (True, True),
    }


def test_importing_every_module_starts_nothing() -> None:
    """Packaging and standalone action runs import the entry module; it must not start the app."""
    script = (
        "import importlib, pkgutil, threading\n"
        "import main, zelos_extension_instruments as package\n"
        "for module in pkgutil.walk_packages(package.__path__, f'{package.__name__}.'):\n"
        "    importlib.import_module(module.name)\n"
        "assert threading.active_count() == 1, threading.enumerate()\n"
    )
    subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, check=True, timeout=60)


def test_the_manifest_keeps_within_the_keyword_limit() -> None:
    """The CLI refuses a manifest with more than 20 keywords."""
    manifest = tomllib.loads((REPO_ROOT / "extension.toml").read_text(encoding="utf-8"))
    keywords = manifest["keywords"]
    assert 1 <= len(keywords) <= 20, len(keywords)
    assert len(set(keywords)) == len(keywords)
