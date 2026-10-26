"""What `zelos extensions package` and the marketplace read, checked before a tag is pushed."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from jsonschema import Draft7Validator

from zelos_extension_instruments import ACTION_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((REPO_ROOT / "extension.toml").read_text(encoding="utf-8"))


def test_entry_module_re_exports_the_action_prefix() -> None:
    """The inventory dump reads the prefix off the entry module and falls back to its file name,
    so a missing re-export ships `main/list_instruments` beside a live `Instruments/...`."""
    import main

    assert main.ACTION_PREFIX == ACTION_PREFIX == MANIFEST["name"] == "Instruments"


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
            *("--entry", MANIFEST["agent"]["entry"], "--out", str(out)),
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
