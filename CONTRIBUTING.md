# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

| Path | Holds |
| ---- | ----- |
| `profiles/<category>/<id>.toml` | One profile per instrument family, in `psu`, `load`, `dmm`, `acsource`, `scope`, `fgen`, `daq` and `generic`. [docs/profiles.md](docs/profiles.md) is the format. |
| `zelos_extension_instruments/profile.py`, `profile_load.py`, `engine.py` | The profile model, the loader that checks a profile file, and the engine that runs any profile. `profile_system.py`, `profile_scope.py` and `profile_daq.py` hold the format's `[system]`, `[scope]` and `[daq]` parts. |
| `zelos_extension_instruments/link/` | Raw TCP, serial and VISA links behind one interface. |
| `zelos_extension_instruments/worker.py` | One thread per instrument: identify, poll, serve requests, reconnect. |
| `zelos_extension_instruments/serve.py`, `system_ops.py`, `scope_ops.py`, `daq_ops.py` | What the worker does for a request: sets and reads, the system actions, scope measurements and waveforms, and data-acquisition scans. |
| `zelos_extension_instruments/sim/` | A fake instrument served from any profile, including generators, scopes and data-acquisition units, with fault hooks, for tests and the demo. |
| `zelos_extension_instruments/record.py` | The transcript recorder. |
| `tests/transcripts/<id>/` | Transcripts recorded from real units; each replays against its profile in CI. |
| `tests/hardware/` | The nightly bench job's tests and scripts. |

## Commands

| Command                           | Description                                                      |
| --------------------------------- | ---------------------------------------------------------------- |
| `just install`                    | Install dependencies and the pre-commit hooks                    |
| `just format`                     | Format code with ruff                                            |
| `just check`                      | Check formatting, lint (ruff), type-check (pyright)              |
| `just test`                       | Run tests (pytest)                                               |
| `just profiles [paths]`           | Check every shipped profile, and any paths given                 |
| `just sim <profile> [--port N]`   | Serve a fake instrument from a profile on a TCP port             |
| `just record <profile> <address>` | Record a transcript from a real instrument                       |
| `just run`                        | Run the agent runtime locally against `./config.json`            |
| `just package`                    | Package for the Zelos marketplace                                |
| `just release VERSION`            | Bump version, check, test, commit, tag                           |
| `just clean`                      | Remove build output, caches and packages                         |

`just install` installs the pre-commit hooks in `.pre-commit-config.yaml`. `just record --set`
rewrites each level to its current value, and never sends a switch, label or command.

## Testing

`just test` skips the cadence gates and the bench tests. `uv run pytest -m perf` runs the cadence
gates. CI runs `just check` and `just test` on Linux, macOS and Windows for every push and pull
request. The nightly Soak workflow runs the cadence and memory gates for an hour. The nightly
Hardware workflow runs on the self-hosted bench runner against the instruments its `BENCH`
variable lists.

## Run it in Zelos

```bash
zelos extensions install-local .
zelos extensions start local.instruments --config '{"instruments": [{"connection": "demo", "category": "psu"}]}'
zelos extensions logs local.instruments
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall local.instruments`.
- `extension.toml`: run `zelos extensions install-local .` again.

To run it outside the agent, write a `config.json` and use `just run`. It connects to the agent
at `ZELOS_AGENT_URL`, else `http://localhost:2300`:

```bash
echo '{"instruments": [{"connection": "demo", "category": "psu"}, {"connection": "demo", "category": "load"}]}' > config.json
just run
```

## Try a profile without hardware

```bash
just sim rigol-dp800 --port 5555
```

Then configure a LAN instrument at `127.0.0.1:5555` with the profile `rigol-dp800`. The fake answers every query the profile makes, keeps setpoints, models a resistive load, answers the system functions, keeps a generator's settings and a data-acquisition unit's scan and relays, and fills an error queue for unknown commands and out-of-range values. It proves that the engine executes the profile; only a transcript from a real unit proves the profile describes the instrument. A data-acquisition unit also needs its Scan and Relays settings: see [docs/reference.md](docs/reference.md#data-acquisition-units).

## Add an instrument

1. Copy the closest profile in `profiles/<category>/` and transcribe the commands from the instrument's programming manual. Never guess a command: leave out what the manual does not give, and say so in `notes`.
2. `just profiles` until it passes.
3. `just sim <id>` and configure the fake, or configure the real unit, and watch the signals arrive.
4. `just record <id> <address>` on the real unit, and commit the transcript with the profile. Set the badge to `transcript`.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release.
