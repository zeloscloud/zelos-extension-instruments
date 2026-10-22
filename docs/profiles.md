# Profiles

A profile is one TOML file that tells the extension how to talk to one instrument family: how to recognise it, what to read, what can be set, and what the instrument's replies mean. The shipped profiles live in `profiles/<category>/`. Your own load the same way (see [Your own profiles](#your-own-profiles)).

Every command string comes from the instrument's programming manual. A profile never contains code.

## A complete example

```toml
schema = 1
id = "rigol-dp800"
name = "Rigol DP800 series"
category = "psu"
notes = "The limits are the rated values of each channel from Table 2-1 of the programming guide, and the instrument accepts settings a little above them: 0 V to 32 V and 0 A to 3.2 A on the DP832 CH1 and CH2, for example. The DP831 CH3 is negative, so its voltage runs from -30 V to 0 V. The DP811 and DP813 have two ranges, chosen with :OUTP:RANG, and the limits here take the larger voltage and the larger current of the two. The guide gives the settable values of the active range only. OVP and OCP are off by default, so enable them with the OVP and OCP switches here or on the front panel before relying on the trip flags. The OVP and OCP levels take the settable ranges of Table 2-2 with 0.01 as the smallest level, because the A models accept 1 mV and the others 10 mV. The models without the A suffix list 2 decimal places unless the high resolution option is installed, and this profile sends 3. Remote sense is read where the channel has the Sense function; the guide says other channels reply NONE, so they are not recorded. SYST:REMOTE leaves only the output key of each channel, the power switch and Back usable on the front panel, and SYST:LOCAL returns control. The guide says commands sent over RS232 end with CR LF, and this profile sends a newline, so set the write terminator in the config for an RS232 link. The programming guide does not name the LAN socket port, so 5555 is unverified."

[evidence]
badge = "manual"
source = "RIGOL DP800 Series Programmable Linear DC Power Supply Programming Guide, PGH03109-1110, Jun. 2021, software version 00.01.17"
url = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/dc-powers/DP800_ProgrammingGuide_EN.pdf"
date = "2026-10-21"

[match]
manufacturer = '(?i)^rigol'
model = '^DP8(11|13|21|22|31|32)A?$'
channels = 1

[[match.models]]
model = '^DP82'
channels = 2

[[match.models]]
model = '^DP83'
channels = 3

[link]
lan_port = 5555
baud = 9600
remote = "SYST:REMOTE"
local = "SYST:LOCAL"
locks_panel = true

[errors]
query = "SYST:ERR?"

# Programming Guide Table 2-1 (voltage and current) and Table 2-2 (OVP and OCP), rated channel values
[ranges]
voltage = { min = 0, max = 30, digits = 3 }
current = { min = 0, max = 3, digits = 3 }
ovp = { min = 0.01, max = 33, digits = 3 }
ocp = { min = 0.01, max = 3.3, digits = 3 }

[[ranges.models]]
model = '^DP832'
channel = 3
voltage = { min = 0, max = 5, digits = 3 }
ovp = { min = 0.01, max = 5.5, digits = 3 }

[[ranges.models]]
model = '^DP831'
channel = 1
voltage = { min = 0, max = 8, digits = 3 }
current = { min = 0, max = 5, digits = 3 }
ovp = { min = 0.01, max = 8.8, digits = 3 }
ocp = { min = 0.01, max = 5.5, digits = 3 }

[[ranges.models]]
model = '^DP831'
channel = 2
current = { min = 0, max = 2, digits = 3 }
ocp = { min = 0.01, max = 2.2, digits = 3 }

[[ranges.models]]
model = '^DP831'
channel = 3
voltage = { min = -30, max = 0, digits = 3 }
current = { min = 0, max = 2, digits = 3 }
ovp = { min = -33, max = -0.01, digits = 3 }
ocp = { min = 0.01, max = 2.2, digits = 3 }

[[ranges.models]]
model = '^DP822'
channel = 1
voltage = { min = 0, max = 20, digits = 3 }
current = { min = 0, max = 5, digits = 3 }
ovp = { min = 0.01, max = 22, digits = 3 }
ocp = { min = 0.01, max = 5.5, digits = 3 }

[[ranges.models]]
model = '^DP822'
channel = 2
voltage = { min = 0, max = 5, digits = 3 }
current = { min = 0, max = 16, digits = 3 }
ovp = { min = 0.01, max = 5.5, digits = 3 }
ocp = { min = 0.01, max = 16.8, digits = 3 }

[[ranges.models]]
model = '^DP821'
channel = 1
voltage = { min = 0, max = 60, digits = 3 }
current = { min = 0, max = 1, digits = 3 }
ovp = { min = 0.01, max = 66, digits = 3 }
ocp = { min = 0.01, max = 1.1, digits = 3 }

[[ranges.models]]
model = '^DP821'
channel = 2
voltage = { min = 0, max = 8, digits = 3 }
current = { min = 0, max = 10, digits = 3 }
ovp = { min = 0.01, max = 8.8, digits = 3 }
ocp = { min = 0.01, max = 11, digits = 3 }

[[ranges.models]]
model = '^DP813'
voltage = { min = 0, max = 20, digits = 3 }
current = { min = 0, max = 20, digits = 3 }
ovp = { min = 0.01, max = 22, digits = 3 }
ocp = { min = 0.01, max = 22, digits = 3 }

[[ranges.models]]
model = '^DP811'
voltage = { min = 0, max = 40, digits = 3 }
current = { min = 0, max = 10, digits = 3 }
ovp = { min = 0.01, max = 44, digits = 3 }
ocp = { min = 0.01, max = 11, digits = 3 }

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

[state.ovp_set]
query = ":OUTP:OVP:VAL? CH{ch}"
parse = "float"

[state.ocp_set]
query = ":OUTP:OCP:VAL? CH{ch}"
parse = "float"

[state.ovp_on]
query = ":OUTP:OVP? CH{ch}"
parse = "bool"

[state.ocp_on]
query = ":OUTP:OCP? CH{ch}"
parse = "bool"

[state.remote_sense]
query = ":OUTP:SENS? CH{ch}"
parse = "bool"
optional = true

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

[setters.ovp]
command = ":OUTP:OVP:VAL CH{ch},{value}"

[setters.ocp]
command = ":OUTP:OCP:VAL CH{ch},{value}"

[setters.ovp_on]
command = ":OUTP:OVP CH{ch},{on}"

[setters.ocp_on]
command = ":OUTP:OCP CH{ch},{on}"

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
| `category` | yes | `psu`, `load`, `dmm`, `acsource` or `scope`. It fixes which field names are allowed. |
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
| `first` | none | A command sent before the identity query, for an instrument that answers nothing until it has one: the address of a multi-drop link (`ADR 6`). It may not change an output. |
| `needs_remote` | `false` | Queries fail unless the instrument is in remote mode (some meters refuse `READ?` in local), so the panel is never handed back while the extension runs. |
| `device_clear` | `true` | `false` when a VISA device clear hangs the instrument; the extension then only drops its read buffer. |
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
| `query` | The command. `{ch}` is the channel number, from 1; `{ch0}` the same channel numbered from 0, for instruments that count that way. |
| `parse` | How the reply is read. See [Parsers](#parsers). |
| `fields` | For `parse = "floats"`: the field each value lands in, in order. |
| `index` | For `parse = "float"` or `"int"`: which comma-separated value to take, from 0. |
| `map` | For `parse = "map"` and `"bits"`: reply text (or bit value) to the field's label. |
| `mask`, `shift` | For `parse = "bits"`: a whole number, or a list with one per channel when the instrument packs every channel into one status word (`mask = [0x10, 0x20]`). |
| `format` | For `parse = "bits"` and `"int"`: `"int"` (default) or `"hex"`. |
| `scale` | For `parse = "float"` and `"floats"`: multiplies the number, so a slew rate the instrument states in A/µs lands in A/s with `scale = 1e6`. |
| `flags` | For `protection`: a list of `{ query, parse, value }`; the first that reads true sets the field, else `none`. |
| `optional` | `true` lets a generic profile probe: a query the instrument rejects at start is dropped with one log line. |
| `channel` | `false` for a query that is per instrument, not per channel (an over-temperature flag). Default `true`. A reader of an instrument-wide field (a scope's `timebase`) is per instrument whatever this says. |

### Fields by category

| Category | `[signals]` | `[state]` |
|---|---|---|
| `psu` | `voltage` V, `current` A, `power` W | `voltage_set` V, `current_set` A, `ovp_set` V, `ocp_set` A, `ovp_on`, `ocp_on`, `output`, `mode`, `protection`, `remote_sense`, `tracking` |
| `load` | `voltage` V, `current` A, `power` W, `resistance` Ω | `current_set` A, `voltage_set` V, `power_set` W, `resistance_set` Ω, `slew_set` A/s, `current_range`, `output` (input on), `mode`, `protection` |
| `acsource` | `voltage` V rms, `current` A rms, `power` W, `apparent_power` VA, `power_factor`, `frequency` Hz | `voltage_set` V, `frequency_set` Hz, `current_set` A, `output`, `protection` |
| `dmm` | `value` (the reading, in the active function's unit) | `function`, `range`, `autorange`, `nplc` |
| `scope` | the measurements: `pkpk`, `maximum`, `minimum`, `amplitude`, `top`, `base`, `mean`, `cycle_mean`, `rms`, `cycle_rms`, `stdev` V, `overshoot`, `preshoot`, `duty` %, `period`, `positive_width`, `negative_width`, `rise_time`, `fall_time` s, `frequency` Hz | per channel: `visible`, `scale` V, `offset` V, `coupling`, `bandwidth_limit`, `probe`; per instrument: `timebase` s, `delay` s, `sample_rate` Sa/s, `memory_depth` pts, `points` pts, `acquisitions`, `trigger_status`, `trigger_mode`, `trigger_source`, `trigger_level` V, `trigger_slope`, `trigger_frequency` Hz |

`mode` labels: `off`, `cv`, `cc`, `cp`, `cr`, `unregulated`, `unknown`. For a load, `mode` is the configured function.
`protection` labels: `none`, `ovp`, `ocp`, `opp`, `otp`, `other`.
`function` labels (DMM): `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`, `resistance`, `resistance_4w`, `frequency`, `period`, `temperature`, `capacitance`, `diode`, `continuity`, `other`.
`tracking` labels: `independent`, `series`, `parallel`, `unknown`. `current_range` labels: `low`, `mid`, `high`, `auto`, `unknown`.
Scope labels: `coupling` `dc`, `ac`, `gnd`; `bandwidth_limit` `full`, `20mhz`, `25mhz`, `100mhz`, `200mhz`, `250mhz`, `other`; `trigger_status` `armed`, `ready`, `auto`, `triggered`, `stopped`, `roll`; `trigger_mode` `auto`, `normal`, `single`, `forced`; `trigger_source` `ch1`…`ch8`, `line`, `external`, `digital`; `trigger_slope` `rising`, `falling`, `either`. Each list ends in `unknown` for a reply the map lacks.

A scope's measurements are not read with fixed queries: the profile's `[scope.slots]` says how the instrument's own measurement slots are read, and whatever a slot shows lands under its source channel. The instrument-wide fields land in `<name>/acquisition`.

A DMM's `value` is recorded under the active function, as `<name>/dc_voltage.dc_voltage`, so each function keeps its unit and a plot legend names it.

## `[setters]`

Keyed by what the actions change. Values: `voltage`, `current`, `power`, `resistance`, `frequency`, `ovp`, `ocp`, `slew`, and for a scope `scale`, `offset`, `probe`, `timebase`, `delay`, `trigger_level`. Switches: `output`, `ovp_on`, `ocp_on`, `visible`. Labels: `function`, and for a scope `coupling`, `trigger_source`, `trigger_slope`, `trigger_mode`. Commands: `clear`, and for a scope `run`, `stop`, `single`, `autoset`.

| Key | Meaning |
|---|---|
| `command` | The command. Placeholders: `{ch}`, `{value}` (formatted with the range's digits), `{on}` (`ON`/`OFF`), `{bit}` (`1`/`0`), `{function}` (through `map`). `clear` may give a list of commands, sent in order. |
| `verify` | The readback query. Defaults to the matching `[state]` entry's query (`voltage` verifies with `state.voltage_set`). A set fails when the readback differs by more than one step. |
| `range` | Which `[ranges]` key bounds the value. Defaults to the setter's name. |
| `map` | For `function` and the label setters: our label to the instrument's word (`cc = "CURR"`, `rising = "RISing"`). A label setter's command carries the word as `{value}`. |
| `channel` | `false` for an instrument-wide command. |
| `scale` | For a value setter: the value is sent divided by it, so a slew rate the instrument takes in A/µs goes out right with `scale = 1e6` (its `[state]` reader carries the same `scale`). |

## `[scope]`

Only for `category = "scope"`. `grid` is the number of horizontal divisions (10 on most scopes), which places a capture's samples in time.

### `[scope.slots]`

How the instrument's measurement slots are read. Which slots are on, what each measures and from which channel is read from the instrument every second; the extension never turns a measurement on by itself.

| Key | Meaning |
|---|---|
| `count` | How many slots the instrument has. |
| `enabled`, `type`, `source`, `value` | The queries for one slot, with `{slot}`: whether it is on (`bool`), what it measures (the instrument's word), its source, and its value (`float`). |
| `source_pattern` | A regular expression on the source reply that captures the channel number as `(?P<ch>…)`. A source it does not match (a math trace) is not recorded. |
| `types` | The instrument's measurement word to our field (`PKPK = "pkpk"`). A word not listed is not recorded. |
| `ready` | Optional `{ query, value }`: the slots are read only while the query answers `value` (a measurement mode). |

### `[scope.measure]`

The Measure action. `commands` is the list sent to show a measurement, with `{slot}`, `{ch}` and `{type}` (the word from `types`); `off` turns a slot off.

### `[scope.waveform]`

The Capture Waveform action. `setup` is the list of commands that select the source and the transfer (`{ch}`, `{points}`, `{interval}`), `preamble` and `data` are the two block queries, `max_points` and `points` the optional queries for the largest transfer and the points on screen. `format` names the descriptor: `wavedesc`, the LeCroy-style block Siglent SDS scopes return, decoded as the programming guide's table says (vertical gain, offset and code-per-division at bytes 156, 160 and 164, the sample interval at 176, the trigger delay at 180, the probe factor at 328). A sample's time is `delay - timebase * grid / 2 + index * interval`, placed around the moment of the capture.

## `[dmm]`

| Key | Meaning |
|---|---|
| `function_query` | Returns the active function. |
| `function_map` | Reply text to our function label. Include the quotes the instrument returns (`'"VOLT"' = "dc_voltage"`). |
| `functions` | Function label to unit, for every function the profile can read. |

## Parsers

| `parse` | Reply | Result |
|---|---|---|
| `float` | A number in any SCPI form: `12.000`, `+1.23456E-02`, `1.2e3`, with optional trailing unit letters and a metric prefix (`10M`, `1.00GSa/s`, `5mV`). Text before the number (a header such as `C1:VDIV 5.00E-01V`) is a parse error, logged once. `9.9E+37` and larger read as not-a-number, as SCPI defines. | float |
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
