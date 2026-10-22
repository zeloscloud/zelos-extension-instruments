# Contributing

## Prerequisites

- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/)
- [uv](https://docs.astral.sh/uv/) 0.9.5 or later
- [just](https://github.com/casey/just)
- Python 3.11 (uv installs it)

The Zelos guide to [developing extensions](https://docs.zeloscloud.io/sdk/how-to/develop-extensions/) covers the SDK, and [Package and Publish Extensions](https://docs.zeloscloud.io/sdk/how-to/package-extensions/) covers the archive and the marketplace.

## Layout

| Path | Holds |
| ---- | ----- |
| `profiles/<category>/<id>.toml` | One profile per instrument family. [docs/profiles.md](docs/profiles.md) is the format. |
| `zelos_extension_instruments/profile.py`, `engine.py` | The profile model and the engine that runs any profile. |
| `zelos_extension_instruments/link/` | Raw TCP, serial and VISA links behind one interface. |
| `zelos_extension_instruments/worker.py` | One thread per instrument: identify, poll, serve requests, reconnect. |
| `zelos_extension_instruments/sim/` | A fake instrument served from any profile, with fault hooks, for tests and the demo. |
| `zelos_extension_instruments/record.py` | The transcript recorder. |
| `tests/transcripts/<id>/` | Transcripts recorded from real units; each replays against its profile in CI. |
| `tests/hardware/` | The nightly bench job's tests and scripts. |

## Commands

| Command | Description |
| ------- | ----------- |
| `just install` | Install dependencies and the pre-commit hooks |
| `just format` | Format and fix lint with ruff |
| `just check` | Format check, lint and pyright |
| `just test` | Run the tests (`uv run pytest -m perf` adds the cadence gates) |
| `just profiles [paths]` | Check every shipped profile, and any paths given |
| `just sim <profile> [--port N]` | Serve a fake instrument from a profile on a TCP port |
| `just record <profile> <address>` | Record a transcript from a real instrument |
| `just run` | Run the extension against `./config.json` |
| `just package` | Package for the Zelos marketplace |
| `just release VERSION` | Bump version, check, test, commit, tag |
| `just clean` | Remove build output, caches and packages |

CI runs `just check` and `just test` on Linux, macOS and Windows for every push and pull request. The nightly Soak workflow runs the cadence and memory gates for an hour. The nightly Hardware workflow runs on the self-hosted bench runner against the instruments its `BENCH` variable lists.

## Run it in Zelos

```bash
echo '{"instruments": [{"connection": "demo", "category": "psu"}, {"connection": "demo", "category": "load"}]}' > config.json
just run
```

`just run` connects to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`. To run it under the agent instead:

```bash
zelos extensions install-local .
zelos extensions start local.instruments --config '{"instruments": [{"connection": "demo", "category": "psu"}]}'
zelos extensions logs local.instruments
```

## Try a profile without hardware

```bash
just sim rigol-dp800 --port 5555
```

Then configure a LAN instrument at `127.0.0.1:5555` with the profile `rigol-dp800`. The fake answers every query the profile makes, keeps setpoints, models a resistive load, and fills an error queue for unknown commands and out-of-range values. It proves that the engine executes the profile; only a transcript from a real unit proves the profile describes the instrument.

## Add an instrument

1. Copy the closest profile in `profiles/<category>/` and transcribe the commands from the instrument's programming manual. Never guess a command: leave out what the manual does not give, and say so in `notes`.
2. `just profiles` until it passes.
3. `just sim <id>` and configure the fake, or configure the real unit, and watch the signals arrive.
4. `just record <id> <address>` on the real unit, and commit the transcript with the profile. Set the badge to `transcript`.

## Releasing

```bash
just release 0.2.0
git push --follow-tags
```

The tag runs `.github/workflows/release.yml`, which checks, tests, packages and attaches the archive to a GitHub release.
