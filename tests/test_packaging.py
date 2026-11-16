"""What `zelos extensions package` and the marketplace read, checked before a tag is pushed."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from jsonschema import Draft7Validator

import zelos_extension_instruments
from zelos_extension_instruments import ACTION_PREFIX, config
from zelos_extension_instruments.profile_load import SHIPPED_DIR

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((REPO_ROOT / "extension.toml").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_name_is_the_action_prefix() -> None:
    """Live actions register under ACTION_PREFIX; stopped ones under the manifest name."""
    assert MANIFEST["name"] == ACTION_PREFIX == "Instruments"


def test_config_schema_is_valid_and_its_examples_validate() -> None:
    """Draft 7, because it is the draft that enforces the per-connection `dependencies`."""
    schema = json.loads((REPO_ROOT / MANIFEST["config"]["schema"]).read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    assert schema["examples"]
    for example in schema["examples"]:
        validator.validate(example)


def test_the_wheel_holds_the_schema_and_the_profiles_the_package_reads() -> None:
    """A pip install carries only the package directory."""
    package = Path(zelos_extension_instruments.__file__).resolve().parent
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [package.name]
    schema = (REPO_ROOT / MANIFEST["config"]["schema"]).resolve()
    assert schema.is_relative_to(package)
    assert json.loads(schema.read_text(encoding="utf-8")) == config._SCHEMA
    assert SHIPPED_DIR.is_relative_to(package)
    assert any(SHIPPED_DIR.rglob("*.toml"))


def test_inventory_holds_the_three_hidden_read_only_standalone_actions(tmp_path: Path) -> None:
    """The agent serves this file while the extension is stopped; the config form's pickers and
    Auto-configure need all three actions in it."""
    out = tmp_path / "actions.json"
    subprocess.run(
        [
            *(sys.executable, "-m", "zelos_sdk.extensions.actions", "dump"),
            *("--entry", MANIFEST["agent"]["entry"], "--out", str(out)),
        ],
        cwd=REPO_ROOT,
        check=True,
        timeout=60,
    )

    inventory = json.loads(out.read_text(encoding="utf-8"))
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
