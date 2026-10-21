# Profiles

A profile is one TOML file that tells the extension how to talk to one instrument family: how to recognise it, what to read, what can be set, and what the instrument's replies mean. The shipped profiles live in `profiles/<category>/`. Your own load the same way (see [Your own profiles](#your-own-profiles)).

Every command string comes from the instrument's programming manual. A profile never contains code.

## A complete example

```toml
schema = 1
id = "rigol-dp800"
name = "Rigol DP800 series"
category = "psu"

[evidence]
badge = "manual"
source = "Rigol DP800 Programming Guide, PGH05101-1110, 2016"
url = "https://www.batronix.com/pdf/Rigol/DP800_ProgrammingGuide_EN.pdf"
date = "2026-10-20"

[match]
manufacturer = '(?i)^rigol'
model = '^DP8(11|21|31|32)A?$'
channels = 1

[[match.models]]
model = '^DP821'
channels = 2

[[match.models]]
model = '^DP83'
channels = 3

[link]
lan_port = 5555
baud = 9600
remote = "SYST:REMOTE"
local = "SYST:LOCAL"

[errors]
query = "SYST:ERR?"

[ranges]
voltage = { min = 0, max = 30, digits = 3 }
current = { min = 0, max = 3, digits = 3 }

[[ranges.models]]
model = '^DP832'
channel = 3
voltage = { min = 0, max = 5, digits = 3 }

[signals.measure]
query = ":MEAS:ALL? CH{ch}"
parse = "floats"
fields = ["voltage", "current", "power"]

[state.voltage_set]
query = ":SOUR{ch}:VOLT?"
parse = "float"

[state.current_set]
query = ":SOUR{ch}:CURR?"
parse = "float"

[state.output]
query = ":OUTP? CH{ch}"
parse = "bool"

[state.mode]
query = ":OUTP:MODE? CH{ch}"
parse = "map"
map = { CV = "cv", CC = "cc", UR = "unregulated" }

[state.protection]
flags = [
  { query = ":OUTP:OVP:QUES? CH{ch}", parse = "bool", value = "ovp" },
  { query = ":OUTP:OCP:QUES? CH{ch}", parse = "bool", value = "ocp" },
]

[setters.voltage]
command = ":SOUR{ch}:VOLT {value}"

[setters.current]
command = ":SOUR{ch}:CURR {value}"

[setters.output]
command = ":OUTP CH{ch},{on}"

[setters.clear]
command = [":OUTP:OCP:CLEAR CH{ch}", ":OUTP:OVP:CLEAR CH{ch}"]
```

## Top level

| Key | Required | Meaning |
|---|---|---|
| `schema` | yes | The profile format version. `1`. |
| `id` | yes | Lower-case letters, digits and hyphens; the file name without `.toml`. The config's Profile field stores it. |
| `name` | yes | What the app shows. |
| `category` | yes | `psu`, `load`, `dmm` or `acsource`. It fixes which field names are allowed. |
| `notes` | no | Free text shown in Get State: traps, what is not supported, what was not tested. |

## `[evidence]`

| Key | Required | Meaning |
|---|---|---|
| `badge` | yes | `manual`: transcribed from the programming manual, never seen a unit. `transcript`: a transcript recorded from a real unit replays in the tests. `hardware`: runs on the nightly bench. |
| `source` | yes | The document title and revision, or the transcript file. |
| `url` | no | Where the document is. |
| `firmware` | no | The firmware the transcript or bench unit runs. |
| `date` | yes | When the profile was written or last verified, `YYYY-MM-DD`. |

A profile is marked `transcript` only when a transcript file for it exists under `tests/transcripts/<id>/`, and `hardware` only when the nightly bench runs it. The tests check both ways: a badge without its evidence, or evidence without its badge, fails.

## `[match]`

How an identity reply is matched to the profile. `*IDN?` returns four comma-separated fields: manufacturer, model, serial number, firmware. Each field is stripped of spaces before matching.

| Key | Required | Meaning |
|---|---|---|
| `manufacturer` | yes | A regular expression the manufacturer field must match. Use `(?i)` for case-insensitive, and alternation for vendors that spell their name several ways. |
| `model` | yes | A regular expression the model field must match. |
| `idn` | no | The identify command. Default `*IDN?`. |
| `channels` | no | The channel count. Default `1`. |
| `channels_query` | no | A query whose integer reply is the channel count, for a family whose model string does not say. |
| `[[match.models]]` | no | Per-model overrides: `model` (regex) and `channels`. The first matching entry wins. |

## `[link]`

Connection defaults for this family. The config can override each.

| Key | Default | Meaning |
|---|---|---|
| `write_terminator` | `"\n"` | Appended to every command. |
| `read_terminator` | `"\n"` | Ends every reply. |
| `timeout_s` | `2.0` | How long one query may take. |
| `delay_ms` | `0` | A pause the vendor documents between commands. Only set it when the manual or an application note says so. |
| `lan_port` | `5025` | The raw socket port. |
| `baud`, `data_bits`, `parity`, `stop_bits`, `flow_control` | `9600`, `8`, `"none"`, `1`, `"none"` | Serial defaults. |
| `remote` | none | Sent once after connect, for instruments that need it before they accept commands (`SYST:REM`, `SYST:LOCK 1`). |
| `local` | none | Returns front-panel control. The Local action sends it, and so does keep-panel. |
| `locks_panel` | `false` | Remote control locks the front panel, so keep-panel is offered for this family. |
| `select` | none | Sent before a channel's commands when the selected channel changes, for the select-then-command style (`INST:NSEL {ch}`). |
| `init` | `[]` | Commands sent once after connect. They may not change an output: the lint refuses `*RST`, `SYST:PRES`, `OUTP`, `INP` and anything that writes a level. |
| `single_client` | `false` | The instrument accepts one connection at a time. Shown in Get State. |
| `ack` | none | The reply every command that is not a query gets, such as `OK`. The extension reads it after each command and treats anything else as an error. For instruments that answer every command. |

## `[errors]`

| Key | Default | Meaning |
|---|---|---|
| `query` | none | The error-queue query, usually `SYST:ERR?`. Without it the family has no error queue, and sets are verified by readback only. |
| `ok` | `^\s*[+-]?0\b` | A regular expression the reply matches when there is no error. |
| `drain` | `20` | The most entries read in one pass. |

## `[ranges]`

What a setter may be asked for. Keys are `voltage`, `current`, `power`, `resistance` and `frequency`. Each is a table with `min` (default `0`), `max`, `digits` (decimal places the command is formatted with, default `3`) and optional `step`.

`[[ranges.models]]` entries override by `model` regex and optional `channel`. The first matching entry wins for each key.

A range may instead name queries: `voltage = { max_query = "VOLT? MAX", min_query = "VOLT? MIN", digits = 3 }`. They run once after connect.

A setter with no range known refuses to set until the config names a limit for it.

## `[signals]` and `[state]`

`[signals]` are read every poll interval. `[state]` are read once a second. Both hold entries keyed by a field name from the category's vocabulary, or a group keyed by any name with a `fields` list.

| Key | Meaning |
|---|---|
| `query` | The command. `{ch}` is the channel number, from 1. |
| `parse` | How the reply is read. See [Parsers](#parsers). |
| `fields` | For `parse = "floats"`: the field each value lands in, in order. |
| `index` | For `parse = "float"` or `"int"`: which comma-separated value to take, from 0. |
| `map` | For `parse = "map"` and `"bits"`: reply text (or bit value) to the field's label. |
| `mask`, `shift` | For `parse = "bits"`: a whole number, or a list with one per channel when the instrument packs every channel into one status word (`mask = [0x10, 0x20]`). |
| `format` | For `parse = "bits"` and `"int"`: `"int"` (default) or `"hex"`. |
| `flags` | For `protection`: a list of `{ query, parse, value }`; the first that reads true sets the field, else `none`. |
| `optional` | `true` lets a generic profile probe: a query the instrument rejects at start is dropped with one log line. |
| `channel` | `false` for a query that is per instrument, not per channel (an over-temperature flag). Default `true`. |

### Fields by category

| Category | `[signals]` | `[state]` |
|---|---|---|
| `psu` | `voltage` V, `current` A, `power` W | `voltage_set` V, `current_set` A, `ovp_set` V, `ocp_set` A, `output`, `mode`, `protection` |
| `load` | `voltage` V, `current` A, `power` W, `resistance` Ω | `current_set` A, `voltage_set` V, `power_set` W, `resistance_set` Ω, `output` (input on), `mode`, `protection` |
| `acsource` | `voltage` V rms, `current` A rms, `power` W, `apparent_power` VA, `power_factor`, `frequency` Hz | `voltage_set` V, `frequency_set` Hz, `current_set` A, `output`, `protection` |
| `dmm` | `value` (the reading, in the active function's unit) | `function` |

`mode` labels: `off`, `cv`, `cc`, `cp`, `cr`, `unregulated`, `unknown`. For a load, `mode` is the configured function.
`protection` labels: `none`, `ovp`, `ocp`, `opp`, `otp`, `other`.
`function` labels (DMM): `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`, `resistance`, `resistance_4w`, `frequency`, `period`, `temperature`, `capacitance`, `diode`, `continuity`, `other`.

A DMM's `value` lands in the event named after the active function (`<name>/dc_voltage`), so each keeps its unit.

## `[setters]`

Keyed by what the Set, Output and Clear actions change: `voltage`, `current`, `power`, `resistance`, `frequency`, `ovp`, `ocp`, `output`, `function`, `clear`.

| Key | Meaning |
|---|---|
| `command` | The command. Placeholders: `{ch}`, `{value}` (formatted with the range's digits), `{on}` (`ON`/`OFF`), `{bit}` (`1`/`0`), `{function}` (through `map`). `clear` may give a list of commands, sent in order. |
| `verify` | The readback query. Defaults to the matching `[state]` entry's query (`voltage` verifies with `state.voltage_set`). A set fails when the readback differs by more than one step. |
| `range` | Which `[ranges]` key bounds the value. Defaults to the setter's name. |
| `map` | For `function`: our label to the instrument's word (`cc = "CURR"`). |
| `channel` | `false` for an instrument-wide command. |

## `[dmm]`

| Key | Meaning |
|---|---|
| `function_query` | Returns the active function. |
| `function_map` | Reply text to our function label. Include the quotes the instrument returns (`'"VOLT"' = "dc_voltage"`). |
| `functions` | Function label to unit, for every function the profile can read. |

## Parsers

| `parse` | Reply | Result |
|---|---|---|
| `float` | A number in any SCPI form: `12.000`, `+1.23456E-02`, `1.2e3`, with optional trailing unit letters. `9.9E+37` and larger read as not-a-number, as SCPI defines. | float |
| `floats` | Comma-separated numbers. | one float per entry of `fields` |
| `int` | A whole number, decimal or `0x` hex (`format = "hex"` reads bare hex). | int |
| `bool` | `0`, `1`, `ON`, `OFF`, `TRUE`, `FALSE`, `YES`, `NO`, any case. | bool |
| `map` | Text; quotes and spaces stripped; matched case-insensitively against `map`. A reply not in the map reads as `unknown` and is logged once. | label |
| `bits` | An integer; `(value & mask) >> shift`, then `map` keyed by the number as text, or a bool when there is no map. | label or bool |
| `str` | The text as it is. | text |

## Your own profiles

Set an instrument's Profile to the path of your file, or put files in `profiles/` under the extension's data directory. They load at start with the same checks as the shipped ones, and a mistake stops the start with one sentence that names the file and the key.

Check a file at any time:

```bash
uv run python -m zelos_extension_instruments.profile check path/to/my-psu.toml
```

To contribute one, open a pull request with the file and, if you can, a transcript recorded on your unit:

```bash
just record my-psu 192.168.1.40        # identifies, reads every signal, writes tests/transcripts/my-psu/<model>-<firmware>.txt
just record my-psu 192.168.1.40 --set  # also rewrites each setpoint to its current value, to prove the setters
```
