# Profiles

A profile is one TOML file that tells the extension how to talk to one instrument family: how to recognise it, what to read, what can be set, and what the instrument's replies mean. The shipped profiles live in `profiles/<category>/`. Your own load the same way (see [Your own profiles](#your-own-profiles)).

Every command string comes from the instrument's programming manual. A profile never contains code.

## A complete example

```toml
schema = 1
id = "rigol-dp800"
name = "Rigol DP800 series"
category = "psu"
notes = "The limits are the rated values of each channel from Table 2-1 of the programming guide, and the instrument accepts settings a little above them: 0 V to 32 V and 0 A to 3.2 A on the DP832 CH1 and CH2, for example. The DP831 CH3 is negative, so its voltage runs from -30 V to 0 V. The DP811 and DP813 have two ranges, chosen with :OUTP:RANG, and the limits here take the larger voltage and the larger current of the two. The guide gives the settable values of the active range only. OVP and OCP are off by default, so enable them with the OVP and OCP switches here or on the front panel before relying on the trip flags. The OVP and OCP levels take the settable ranges of Table 2-2 with 0.01 as the smallest level, because the A models accept 1 mV and the others 10 mV. The models without the A suffix list 2 decimal places unless the high resolution option is installed, and this profile sends 3. Remote sense is read and set where the channel has the Sense function (CH2 of the DP821 and DP822, and the DP811 and DP813); the guide says other channels reply NONE, so they are not recorded and refuse the setting. The mode field comes from :OUTP:MODE?, which answers CV for a channel that is switched off; the extension records the mode of a switched-off channel as off. The track function (:OUTP:TRAC) keeps the voltages of two channels equal and is not series or parallel pairing, so there is no tracking setting. The *TST? query returns the self-test result of the top board, the bottom board and the fan, such as TopBoard:PASS,BottomBoard:PASS,Fan:PASS, and the self-test passes when every item reads PASS. Save and recall use the state files 1 to 10, and a locked file refuses a save. The display message takes 45 characters, and a longer one is rejected. The display switch needs remote mode, which the profile enters at connect, and the display turns on again at local. Lock sends :SYST:RWL ON, and Unlock sends :SYST:RWL OFF. SYST:REMOTE leaves only the output key of each channel, the power switch and Back usable on the front panel, and SYST:LOCAL returns control. The guide says commands sent over RS232 end with CR LF, and this profile sends a newline, so set the write terminator in the config for an RS232 link. The programming guide does not name the LAN socket port; a DP832 answers on 5555. Owners report that DP832 firmware 00.01.13.00.01 does not trip OCP on CH3, and 00.01.14.00.03 fixes it."

[evidence]
badge = "manual"
source = "RIGOL DP800 Series Programmable Linear DC Power Supply Programming Guide, PGH03109-1110, Jun. 2021, software version 00.01.17"
url = "https://www.rigol.com/dam/global/downloads/brochures/en/program-guide/dc-powers/DP800_ProgrammingGuide_EN.pdf"
date = "2026-10-22"

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

[system]
self_test_ok = '(?i)^(\s*\w+\s*:\s*PASS\s*,?)+$'
save = "*SAV {slot}"
recall = "*RCL {slot}"
slots = { min = 1, max = 10 }
beep = ":SYST:BEEP:IMM"
display = ":DISP {on}"
display_text = ':DISP:TEXT "{text}"'
display_text_max = 45
display_clear = ":DISP:TEXT:CLE"
lock = ":SYST:RWL ON"
unlock = ":SYST:RWL OFF"

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

[setters.remote_sense]
command = ":OUTP:SENS CH{ch},{on}"

[setters.clear]
command = [":OUTP:OCP:CLEAR CH{ch}", ":OUTP:OVP:CLEAR CH{ch}"]
```

## Top level

| Key | Required | Meaning |
|---|---|---|
| `schema` | yes | The profile format version. `1`. |
| `id` | yes | Lower-case letters, digits and hyphens; the file name without `.toml`. The config's Profile field stores it. |
| `name` | yes | What the app shows. |
| `category` | yes | `psu`, `load`, `dmm`, `acsource`, `scope`, `fgen` (function generator) or `daq` (data-acquisition unit). It fixes which field and setter names are allowed. |
| `notes` | no | Free text shown in Get State: traps, what is not supported, what was not tested. |
| `generic` | no | `true` marks a category's fallback: it is chosen by category, never matched by identity, so it takes no `manufacturer` or `model`, and it may read no signal. Default `false`. Generic profiles exist for `psu`, `load`, `dmm`, `acsource` and `fgen`. |

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
| `manufacturer` | yes, unless `generic` | A regular expression the manufacturer field must match. Use `(?i)` for case-insensitive, and alternation for vendors that spell their name several ways. |
| `model` | yes, unless `generic` | A regular expression the model field must match. |
| `idn` | no | The identify command. Default `*IDN?`. |
| `channels` | no | The channel count. Default `1`. |
| `channels_query` | no | A query whose integer reply is the channel count, for a family whose model string does not say. |
| `[[match.models]]` | no | Per-model overrides: `model` (regex) and `channels`. The first matching entry wins. |
| `connections` | no | The connections the family's protocol runs on: a list of `lan`, `usb`, `serial` and `visa`. Default: any. A profile whose list lacks the connection an instrument was found on never matches it, so `connections = ["serial"]` keeps a serial-only protocol off a LAN unit. |

## `[link]`

Connection defaults for this family. The config can override each.

| Key | Default | Meaning |
|---|---|---|
| `write_terminator` | `"\n"` | Appended to every command. |
| `read_terminator` | `"\n"` | Ends every reply. |
| `timeout_s` | `2.0` | How long one query may take. |
| `delay_ms` | `0` | A pause between commands, queries included. Set it when the manual, an application note or a real unit needs it. |
| `command_delay_ms` | `0` | A longer pause after a command that sends no reply, for an instrument that drops a command sent too soon after another. A query is paced by its own reply. |
| `lan_port` | `5025` | The raw socket port. |
| `baud`, `data_bits`, `parity`, `stop_bits`, `flow_control` | `9600`, `8`, `"none"`, `1`, `"none"` | Serial defaults. |
| `remote` | none | Sent once after connect, for instruments that need it before they accept commands (`SYST:REM`, `SYST:LOCK 1`). |
| `local` | none | Returns front-panel control: one command, or a list sent in order. The Local action sends it, and so does keep-panel. |
| `locks_panel` | `false` | Remote control locks the front panel, so keep-panel is offered for this family. |
| `first` | none | A command sent before the identity query, for an instrument that answers nothing until it has one: the address of a multi-drop link (`ADR 6`). It may not change an output. |
| `needs_remote` | `false` | The instrument cannot stay in local while it is polled: some meters refuse `READ?` in local, and some units return to remote at the next command. The panel is handed back only when the extension stops. |
| `device_clear` | `true` | `false` when a VISA device clear hangs the instrument; the extension then only drops its read buffer. |
| `select` | none | Sent before a channel's commands when the selected channel changes, for the select-then-command style (`INST:NSEL {ch}`). |
| `init` | `[]` | A command, or a list, sent once after connect. They may not change an output: the lint refuses `*RST`, `SYST:PRES`, `OUTP`, `INP` and anything that writes a level, and any command that a setter or `[system]` also sends. |
| `single_client` | `false` | The instrument accepts one connection at a time. Shown in Get State. |
| `ack` | none | The reply every command that is not a query gets, such as `OK`. The extension reads it after each command and treats anything else as an error. For instruments that answer every command. |
| `remote_check` | none | A table of `query` and `expect`, asked right after `remote`. The open fails with `the instrument refused remote control` unless the reply equals `expect`: `remote_check = { query = "SYST:LOCK:OWN?", expect = "REMOTE" }`. |
| `skip_blank` | `false` | An empty line that arrives where a reply is expected is dropped, for an instrument that sends one before some replies. |

## `[errors]`

| Key | Default | Meaning |
|---|---|---|
| `query` | none | The error-queue query, usually `SYST:ERR?`, or a list of queries asked in order, each drained and each reply checked against `ok` (`query = ["CMR?", "EXR?"]`). Without it the family has no error queue, and sets are verified by readback only. |
| `ok` | `^\s*[+-]?0\b` | A regular expression the reply matches when there is no error. |
| `drain` | `20` | The most entries read in one pass. |
| `bare` | `false` | The queries answer a bare number, the error code, instead of `code,"text"`. Without it, a bare number at the error query is a late reply, and the replies are put back in step. |

## `[ranges]`

What a setter may be asked for. Keys are `voltage`, `current`, `power`, `resistance`, `frequency`, `ovp`, `ocp` and `slew`; for a supply `ovp_delay`; for a meter `nplc`; for a scope `scale`, `timebase`, `offset`, `averages` and `pulse_width`; and for a generator `amplitude`, `offset`, `high`, `low`, `phase`, `duty`, `symmetry`, `width`, `modulation_depth`, `modulation_deviation`, `phase_deviation`, `modulation_frequency`, `burst_cycles`, `burst_period` and `sweep_time`. Each is a table with `min` (default `0`), `max`, `digits` (decimal places the command is formatted with, default `3`) and optional `step`.

A setter with no range of its own falls back to a related one: `ovp` to `voltage`, `ocp` to `current`, `slew_rise` and `slew_fall` to `slew`, and `sweep_start` and `sweep_stop` to `frequency`. The other keys bound their setter only when the profile gives them, so a scope's `offset`, which has no range, stays unchecked, while a generator's `offset`, whose `min` may be negative, is bounded.

`[[ranges.models]]` entries override by `model` regex and optional `channel`. The first matching entry wins for each key.

A range may instead name queries: `voltage = { max_query = "VOLT? MAX", min_query = "VOLT? MIN", digits = 3 }`. They run once after connect, and `{ch}` is allowed in them. A fixed `min` may go with a `max_query`.

A range may instead list the only values the instrument takes: `nplc = { choices = [0.02, 0.2, 1, 10, 100], digits = 2 }`. A set value must equal one of them, and nothing else is checked. `choices` goes with `digits` only. A multimeter's `range` setter needs no range at all: the meter picks the range that covers the value, and the readback shows the range it chose.

A setter with no range known refuses to set until the config names a limit for it. A scope's value setters without a range, and a multimeter's `range`, are the exceptions.

## `[signals]` and `[state]`

`[signals]` are read every poll interval. `[state]` are read once a second. Both hold entries keyed by a field name from the category's vocabulary, or a group keyed by any name with a `fields` list.

| Key | Meaning |
|---|---|
| `query` | The command. `{ch}` is the channel number, from 1; `{ch0}` the same channel numbered from 0, for instruments that count that way. |
| `parse` | How the reply is read. See [Parsers](#parsers). |
| `fields` | For `parse = "floats"`: the field each value lands in, in order. |
| `index` | For `parse = "float"`, `"int"`, `"bool"`, `"str"`, `"map"` or `"bits"`, and for a protection flag: which comma-separated value to take, from 0. A reply in double quotes, such as `"SIN,+5.0E+03,+3.0E+00"`, is split inside the quotes. |
| `key` | For `parse = "keyed"`: the key whose value fills the field (`key = "FRQ"`). |
| `until` | For `parse = "keyed"`: a token that ends the search for `key`, for a reply whose later section repeats it (`until = "CARR"`). |
| `map` | For `parse = "map"` and `"bits"`: reply text (or bit value) to the field's label. For `parse = "bool"`: an instrument's own words to `"on"` or `"off"` (`EXT = "on"`). The key `"*"` gives the label of every reply the map does not name (`"*" = "other"`); without it such a reply is a parse error. |
| `mask`, `shift` | For `parse = "bits"`: a whole number, or a list with one per channel when the instrument packs every channel into one status word (`mask = [0x10, 0x20]`). |
| `format` | For `parse = "bits"` and `"int"`: `"int"` (default) or `"hex"`. |
| `scale` | For `parse = "float"`, `"floats"` and `"keyed"`: multiplies the number, so a slew rate the instrument states in A/µs lands in A/s with `scale = 1e6`. |
| `flags` | For `protection`: a list of `{ query, parse, value }`; the first that reads true sets the field, else `none`. |
| `optional` | `true` lets a generic profile probe: a query the instrument rejects at start is dropped with one log line. |
| `channel` | `false` for a query that is per instrument, not per channel (an over-temperature flag). Default `true`. A reader of an instrument-wide field (a scope's `timebase`) is per instrument whatever this says. |

### Fields by category

| Category | `[signals]` | `[state]` |
|---|---|---|
| `psu` | `voltage` V, `current` A, `power` W | `voltage_set` V, `current_set` A, `ovp_set` V, `ocp_set` A, `ovp_on`, `ocp_on`, `output`, `mode`, `protection`, `remote_sense`, `tracking`, `ovp_delay_set` s |
| `load` | `voltage` V, `current` A, `power` W, `resistance` Ohm | `current_set` A, `voltage_set` V, `power_set` W, `resistance_set` Ohm, `slew_set` A/s, `slew_rise_set` A/s, `slew_fall_set` A/s, `current_range`, `voltage_range`, `output` (input on), `mode`, `protection`, `short` |
| `acsource` | `voltage` V rms, `current` A rms, `power` W, `apparent_power` VA, `power_factor`, `frequency` Hz | `voltage_set` V, `frequency_set` Hz, `current_set` A, `output`, `protection` |
| `dmm` | `value` (the reading, in the active function's unit) | `function`, `range`, `autorange`, `nplc` |
| `scope` | the measurements: `pkpk`, `maximum`, `minimum`, `amplitude`, `top`, `base`, `mean`, `cycle_mean`, `rms`, `cycle_rms`, `stdev` V, `overshoot`, `preshoot`, `duty` %, `period`, `positive_width`, `negative_width`, `rise_time`, `fall_time` s, `frequency` Hz | per channel: `visible`, `scale` V, `offset` V, `coupling`, `bandwidth_limit`, `probe`; per instrument: `timebase` s, `delay` s, `sample_rate` Sa/s, `memory_depth` pts, `points` pts, `acquisitions`, `trigger_status`, `trigger_mode`, `trigger_source`, `trigger_level` V, `trigger_slope`, `trigger_frequency` Hz, `acquisition_mode`, `averages`, `trigger_type`, `pulse_width` s, `pulse_condition` |
| `fgen` | none | per channel: `output`, `shape`, `frequency_set` Hz, `amplitude_set` Vpp, `offset_set` V, `high_set` V, `low_set` V, `phase_set` deg, `duty_set` %, `symmetry_set` %, `width_set` s, `output_load`, `modulation`, `modulation_type`, `modulation_depth_set` %, `modulation_deviation_set` Hz, `phase_deviation_set` deg, `modulation_frequency_set` Hz, `burst`, `burst_cycles_set`, `burst_period_set` s, `sweep`, `sweep_start_set` Hz, `sweep_stop_set` Hz, `sweep_time_set` s |
| `daq` | none: the readings come from [`[daq]`](#daq) and are named after each channel's function (`dc_voltage` V, `ac_voltage` V, `dc_current` A, `ac_current` A, `resistance` Ohm, `resistance_4w` Ohm, `frequency` Hz, `period` s, `temperature` degC) | per relay channel: `closed` |

`mode` labels: `off`, `cv`, `cc`, `cp`, `cr`, `unregulated`, `unknown`. For a load, `mode` is the configured function. The extension records a supply channel's `mode` as `off` while its output is off, whatever the instrument reports.
`protection` labels: `none`, `ovp`, `ocp`, `opp`, `otp`, `other`.
`function` labels (DMM): `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`, `resistance`, `resistance_4w`, `frequency`, `period`, `temperature`, `capacitance`, `diode`, `continuity`, `other`.
`tracking` labels: `independent`, `series`, `parallel`, `unknown`. `current_range` and `voltage_range` labels: `low`, `mid`, `high`, `auto`, `unknown`.
Function generator labels: `shape` `sine`, `square`, `ramp`, `triangle`, `pulse`, `noise`, `dc`, `arbitrary`, `other`; `output_load` `fifty_ohm`, `high_z`, `other`; `modulation_type` `am`, `fm`, `pm`, `fsk`, `pwm`, `other`.
Scope labels: `coupling` `dc`, `ac`, `gnd`; `bandwidth_limit` `full`, `20mhz`, `25mhz`, `100mhz`, `200mhz`, `250mhz`, `other`; `trigger_status` `armed`, `ready`, `auto`, `triggered`, `stopped`, `roll`; `trigger_mode` `auto`, `normal`, `single`, `forced`; `trigger_source` `ch1`…`ch8`, `line`, `external`, `digital`; `trigger_slope` `rising`, `falling`, `either`; `acquisition_mode` `normal`, `average`, `peak`, `hires`, `envelope`, `other`; `trigger_type` `edge`, `pulse`, `other`; `pulse_condition` `greater`, `less`, `other`. Each list ends in `unknown` or `other` for a reply the map lacks.

A scope's measurements are not read with fixed queries: the profile's `[scope.slots]` says how the instrument's own measurement slots are read, and whatever a slot shows lands under its source channel. A scope that has no slots is asked for each measurement directly: see [`[scope.measure]`](#scopemeasure). The instrument-wide fields land in `<name>/acquisition`.

A function generator has no `[signals]`: a channel's settings are read once a second and recorded as they are. A data-acquisition unit's readings come from `[daq]`.

A DMM's `value` is recorded under the active function, as `<name>/dc_voltage.dc_voltage`, so each function keeps its unit and a plot legend names it.

## `[setters]`

Keyed by what the actions change. A profile gives only the setters its instrument has. Each category takes these:

| Category | Setters |
|---|---|
| `psu` | `voltage`, `current`, `ovp`, `ocp`, `ovp_delay`, `ovp_on`, `ocp_on`, `remote_sense`, `tracking`, `output`, `clear` |
| `load` | `current`, `voltage`, `power`, `resistance`, `slew`, `slew_rise`, `slew_fall`, `function`, `current_range`, `voltage_range`, `short`, `output`, `clear` |
| `acsource` | `voltage`, `frequency`, `current`, `output`, `clear` |
| `dmm` | `function`, `range`, `autorange`, `nplc`, `clear` |
| `scope` | `scale`, `offset`, `probe`, `timebase`, `delay`, `trigger_level`, `averages`, `pulse_width`, `coupling`, `bandwidth_limit`, `trigger_source`, `trigger_slope`, `trigger_mode`, `trigger_type`, `pulse_condition`, `acquisition_mode`, `visible`, `run`, `stop`, `single`, `autoset`, `force`, `clear` |
| `fgen` | `output`, `shape`, `frequency`, `amplitude`, `offset`, `high`, `low`, `phase`, `duty`, `symmetry`, `width`, `output_load`, `modulation`, `modulation_type`, `modulation_depth`, `modulation_deviation`, `phase_deviation`, `modulation_frequency`, `burst`, `burst_cycles`, `burst_period`, `sweep`, `sweep_start`, `sweep_stop`, `sweep_time`, `trigger`, `clear` |
| `daq` | `relay`, `clear` |

A setter is one of four kinds. Values take a number: `voltage`, `current`, `power`, `resistance`, `frequency`, `ovp`, `ocp`, `ovp_delay`, `slew`, `slew_rise`, `slew_fall`, `range`, `nplc`, a scope's `scale`, `offset`, `probe`, `timebase`, `delay`, `trigger_level`, `averages` and `pulse_width`, and a generator's `amplitude`, `offset`, `high`, `low`, `phase`, `duty`, `symmetry`, `width`, `modulation_depth`, `modulation_deviation`, `phase_deviation`, `modulation_frequency`, `burst_cycles`, `burst_period`, `sweep_start`, `sweep_stop` and `sweep_time`. Switches are `output`, `ovp_on`, `ocp_on`, `remote_sense`, `short`, `autorange`, `visible`, `modulation`, `burst`, `sweep` and `relay`. Labels are `function`, `tracking`, `current_range`, `voltage_range`, `shape`, `output_load`, `modulation_type` and, for a scope, `coupling`, `bandwidth_limit`, `trigger_source`, `trigger_slope`, `trigger_mode`, `trigger_type`, `pulse_condition` and `acquisition_mode`. Commands are `clear`, a generator's `trigger` and, for a scope, `run`, `stop`, `single`, `autoset` and `force`.

| Key | Meaning |
|---|---|
| `command` | The command. Placeholders: `{ch}`, `{value}` (formatted with the range's digits), `{on}` (`ON`/`OFF`), `{bit}` (`1`/`0`), `{function}` (through `map`), and the ones in the next table. A command setter may give a list of commands, sent in order, and is per channel unless `channel = false`. |
| `off` | For a switch: the command that switches it off, when `command` only switches it on. Neither then needs `{on}` or `{bit}`: `command = "ROUT:CLOSE (@{ch})"`, `off = "ROUT:OPEN (@{ch})"`. |
| `verify` | The readback query. Defaults to the query of the setter's `[state]` field: `<name>_set` when the category has that field, else `<name>` (`voltage` verifies with `state.voltage_set`, `output` with `state.output`). Two names differ: a load's `function` reads back `mode`, and `relay` reads back `closed`. A set fails when the readback differs by more than one step. A label or switch setter fails when the readback is another label or state. |
| `range` | Which `[ranges]` key bounds the value. Defaults to the setter's name, or the fallback in [`[ranges]`](#ranges). |
| `map` | For `function` and the label setters: our label to the instrument's word (`cc = "CURR"`, `rising = "RISing"`). A label setter's command carries the word as `{value}`. For a meter's `range`, `autorange` and `nplc`: function label to the root word that fills `{function}`. |
| `channel` | `false` for an instrument-wide command. |
| `scale` | For a value setter: the value is sent divided by it, so a slew rate the instrument takes in A/µs goes out right with `scale = 1e6` (its `[state]` reader carries the same `scale`). |
| `master` | For `output`, when a channel delivers power only while a master switch is also on: a table of `command` (sets the master, with `{on}` or `{bit}`), `query` (reads it) and `armed` (reads the selected channel's own switch). Switching a channel on sends its command, then raises the master if it is off. That is refused before anything is sent when another channel is armed, because the master would switch it on too. Switching a channel off sends only its command. |

These placeholders also fill setter commands and `verify` queries:

| Placeholder | In | Fills |
|---|---|---|
| `{function}` | a meter's `range`, `autorange` and `nplc` | The root word of the meter's current function: from the setter's own `map`, else from `[dmm]`'s `function_map`. |
| `{src}` | any scope setter | The number of the channel the scope triggers on. `TRIGger:A:LEVel:CH{src}` sets the level of that channel. |
| `{modulation}` | a generator's setters | The instrument's word for the channel's current `modulation_type`, from the `modulation_type` setter's `map`. `SOUR{ch}:{modulation}:STAT {bit}` switches the current type on. |

A label setter with no readback is remembered as set, so a later setter can name it with `{modulation}`. A set that gives a `modulation_type` or a `trigger_source` with other values sends it first. The result of a label with no readback says `unverified`.

## `[scope]`

Only for `category = "scope"`. `grid` is the number of horizontal divisions (10 on most scopes, 14 on the older Siglent), which places a capture's samples in time. A scope profile gives `[scope.slots]` and `[scope.measure]` for a scope with measurement slots, or `[scope.measure]` with a `query` for one without.

### `[scope.slots]`

How the instrument's measurement slots are read. Which slots are on, what each measures and from which channel is read from the instrument every second; the extension never turns a measurement on by itself.

| Key | Meaning |
|---|---|
| `count` | How many slots the instrument has, or the most the extension uses. |
| `enabled` | The query for one slot, with `{slot}`: whether it is on (`bool`). Give `enabled` or `list`, not both. |
| `list`, `list_pattern` | For an instrument whose measurements are added and deleted: one query that names every measurement, and a regular expression that captures each slot number as `(?P<slot>…)` (`MEAS(?P<slot>\d+)`). A slot is on when the reply names it. |
| `type`, `source`, `value` | The queries for one slot, with `{slot}`: what it measures (the instrument's word), its source, and its value (`float`). |
| `source_pattern` | A regular expression on the source reply that captures the channel number as `(?P<ch>…)`. A source it does not match (a math trace) is not recorded. |
| `types` | The instrument's measurement word to our field (`PKPK = "pkpk"`), as a `[scope.slots.types]` table. A word not listed is not recorded. The first word listed for a field is the one sent. |
| `ready` | Optional `{ query, value }`: the slots are read only while the query answers `value` (a measurement mode). |

### `[scope.measure]`

The Measure action. A scope with slots gives `commands`, the list sent to show a measurement, with `{slot}`, `{ch}` and `{type}` (the word from `types`), and `off`, which turns a slot off. When the slots are listed, `commands` must contain `{slot}` and `{ch}`: they create the slot and say what it measures.

A scope without slots gives a `query` instead, and no `[scope.slots]`: the extension asks for each measurement itself, and records the ones the Measure action added.

| Key | Meaning |
|---|---|
| `query` | The command that returns one measurement, with `{type}` and `{ch}`: `:MEASure:{type}? CHANnel{ch}`. |
| `words` | A `[scope.measure.words]` table: our measurement field to the instrument's word for `{type}` (`pkpk = "VPP"`). A measurement not listed is not offered. |
| `index` | Which comma-separated item of the reply holds the value, from 0, for a reply such as `PKPK,5.1V`. |

### `[scope.waveform]`

The Capture Waveform action. `setup` is a command or a list that selects the source and the transfer (`{ch}`, `{points}`, `{interval}`), `preamble` and `data` are the queries (`{ch}` and `{points}` allowed), and `max_points` and `points` are the optional queries for the largest transfer and the points on screen.

`format` names the descriptor. A sample's time is placed around the moment of the capture. `skip` is the number of bytes the instrument sends before the data block, such as a `DAT2,` label, in either format.

- `wavedesc`, the default: the LeCroy-style block Siglent SDS scopes return, decoded as the programming guide's table says (vertical gain, offset and code-per-division at bytes 156, 160 and 164, the sample interval at 176, the trigger delay at 180, the probe factor at 328). A sample's time is `delay - timebase * grid / 2 + index * interval`. `preamble` is required.
- `linear`: samples are codes scaled by six terms. A voltage is `(code - y_ref) * y_mult + y_zero`. A time is `(i - x_ref) * x_inc + x_zero`, in seconds from the trigger. `encoding` is `i1`, `u1`, `i2` or `u2` (`u1` by default) and `byte_order` is `big` or `little` (`little` by default). The six terms `x_inc`, `x_zero`, `x_ref`, `y_mult`, `y_zero` and `y_ref` are required. `preamble` is optional here: it is a query with a comma-separated reply, and a term may take a field of it.

A term is a number, a table, or a list of tables that add up. A table has exactly one of `value` (a number), `field` (the number of an item in the preamble reply, from 0) or `query` (a query whose reply is a number, with `{ch}` and `{points}`). It may also have `invert` (take 1/x), `multiply`, `divide`, `negate` and `times_interval` (multiply by the `{interval}` the capture chose). They apply in that order.

```toml
[scope.waveform]
format = "linear"
setup = [":WAVeform:SOURce CHANnel{ch}", ":WAVeform:FORMat WORD", ":WAVeform:BYTeorder LSBFirst", ":WAVeform:POINts {points}"]
preamble = ":WAVeform:PREamble?"
data = ":WAVeform:DATA?"
encoding = "u2"
byte_order = "little"
x_inc = { field = 4 }
x_zero = { field = 5 }
x_ref = { field = 6 }
y_mult = { field = 7 }
y_zero = { field = 8 }
y_ref = { field = 9 }
```

## `[dmm]`

| Key | Meaning |
|---|---|
| `function_query` | Returns the active function. |
| `function_map` | Reply text to our function label. Include the quotes the instrument returns (`'"VOLT"' = "dc_voltage"`). |
| `functions` | Function label to unit, for every function the profile can read. |

A meter's `range`, `autorange` and `nplc` setters name the function they act on with `{function}`: `command = "{function}:NPLC {value}"`, with a `map` of function label to root word. Their `verify` queries may use `{function}` too.

## `[system]`

The functions every instrument has beside its readings and settings. The Reset, Reboot, Self-Test, Save State, Recall State, Lock Panel, Beep, Display and Screenshot actions run these commands, and only when you run the action. A command here may not also be in `[link]` `init` or `first`. A profile with no `[system]` has the IEEE 488.2 defaults. A generic profile has only those. Set a key to `false` when the instrument has none.

```toml
[system]
self_test = false
save = "*SAV {slot}"
recall = "*RCL {slot}"
slots = { min = 1, max = 10 }
beep = ":SYST:BEEP"
display = ":DISP {on}"
display_text = ':DISP:TEXT "{text}"'
display_text_max = 45
display_clear = ":DISP:TEXT:CLE"
lock = ":SYST:RWL ON"
unlock = ":SYST:RWL OFF"
```

| Key | Default | Meaning |
|---|---|---|
| `reset` | `*RST` | The reset command, such as `*RST` or a family's own word (`RST`). |
| `self_test` | `*TST?` | The self-test query. |
| `self_test_ok` | `^\s*\+?0\s*$` | A regular expression the reply matches when the test passes. |
| `self_test_timeout_s` | `30` | How long the test may take, from 1 to 600 seconds. |
| `opc` | `*OPC?` | A query asked after Reset and Recall State until it answers, so the extension knows the instrument has finished. |
| `settle_timeout_s` | `10` | How long a reset or recall may take, from 1 to 120 seconds. |
| `save`, `recall` | none | The save and recall commands, with `{slot}`. Both need `slots`. |
| `slots` | none | `{ min = 1, max = 10 }`: the first and last slot, as the manual gives them. |
| `beep` | none | The command that sounds the beeper. |
| `display` | none | The command that switches the display, with `{on}` (`ON` or `OFF`) or `{bit}` (`1` or `0`). |
| `display_text` | none | The command that shows text, with `{text}`. It needs `display_text_max`. |
| `display_text_max` | none | The most characters the display takes. A longer text is refused. |
| `display_clear` | none | The command that clears the text. |
| `lock`, `unlock` | none | The commands that lock and unlock the front panel. They come together. While Lock Panel holds the panel locked, `lock` is sent again right after each `remote` the extension sends. |
| `reboot` | none | The command that restarts the instrument. |
| `reboot_s` | `30` | How long a reboot takes before the extension reconnects, from 1 to 600 seconds. |
| `screenshot` | none | The command that returns the screen as an image, or a list of commands whose last one returns it. |
| `screenshot_framing` | `block` | How the image arrives: `block` (an IEEE definite-length block), `bmp` (a raw BMP file) or `png` (a raw PNG file). |
| `screenshot_cleanup` | none | A command, or a list, sent after the image arrives, such as one that deletes a temporary file. |
| `setup_query` | none | A query that returns the instrument's whole setup as a definite-length block. Save State to a file needs it. |
| `setup_load` | none | The text the setup block is written right after. Recall State from a file needs it. |

## `[daq]`

Only for `category = "daq"`, and required there. A data-acquisition unit has no fixed channels: the instrument's Scan and Relays settings list the addresses to use, and this table says how the unit is told to scan them and how a scan is read. A daq profile has no `[signals]`. Each scan channel's reading is recorded under its function's name, and a relay channel's state as `closed`.

At connect, after `init`, and again after a reset or a recall, the extension sends the `configure` commands once per function group, then the `nplc` command once per integration time, then the `scan` commands. Each reading then sends the `read` commands. These are setup commands: none changes an output.

| Key | Required | Meaning |
|---|---|---|
| `scan` | yes | A command, or a list sent in order, that sets the scan list. One command contains `{channels}`, the addresses as a comma-separated list (`101,102,105`). `{count}` is the number of channels. |
| `read` | yes | A command, or a list sent in order, for one scan. The last is the query whose reply is one reading per channel, comma-separated, in ascending address order. Only the last may be a query. `{count}` is allowed. |
| `channel_pattern` | yes | A regular expression the whole address must match, such as `[1-3]\d\d`. A configured channel that does not match stops the connection. |
| `[daq.configure]` | yes | A table of function label to the command, or list, that sets a group of channels to that function. Every command contains `{channels}`. A `temperature` command contains `{tc}` in one command, the thermocouple type letter. Name only the functions the unit reads: `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`, `resistance`, `resistance_4w`, `frequency`, `period` and `temperature`. |
| `[daq.nplc]` | no | `command` sets the integration time of a group of channels, with `{channels}`, `{value}` and `{function}`. `map` gives each function label that takes an integration time and the instrument's word for it, which fills `{function}`. A function not in `map` takes none. |

```toml
[daq]
scan = "ROUT:SCAN (@{channels})"
read = "READ?"
channel_pattern = '[1-3](0[1-9]|[1-3]\d|4[0-8])'

[daq.configure]
dc_voltage = "CONF:VOLT:DC AUTO,DEF,(@{channels})"
temperature = "CONF:TEMP TC,{tc},(@{channels})"

[daq.nplc]
command = "SENS:{function}:NPLC {value},(@{channels})"
map = { dc_voltage = "VOLT:DC", temperature = "TEMP" }

[setters.relay]
command = "ROUT:CLOSE (@{ch})"
off = "ROUT:OPEN (@{ch})"
verify = "ROUT:CLOSE? (@{ch})"
```

The `relay` setter closes and opens a relay channel, with `{ch}` as the address. Its `verify` query reads the relay back. A `[state.closed]` reader (with `parse = "bits"` and a `mask` when the reply is a number) reads a relay once a second. Without one, the `verify` query is read as on or off.

## Parsers

| `parse` | Reply | Result |
|---|---|---|
| `float` | A number in any SCPI form: `12.000`, `+1.23456E-02`, `1.2e3`, with optional trailing unit letters and a metric prefix (`10M`, `1.00GSa/s`, `5mV`). Text before the number (a header such as `C1:VDIV 5.00E-01V`) is a parse error, logged once. `9.9E+37` and larger read as not-a-number, as SCPI defines. | float |
| `floats` | Comma-separated numbers. | one float per entry of `fields` |
| `int` | A whole number, decimal or `0x` hex (`format = "hex"` reads bare hex). | int |
| `bool` | `0`, `1`, `ON`, `OFF`, `TRUE`, `FALSE`, `YES`, `NO`, any case, and the words in `map`. | bool |
| `map` | Text; quotes and spaces stripped; matched case-insensitively against `map`. A reply not in the map takes the label of the `"*"` key; without one it reads as `unknown` and is logged once. | label |
| `bits` | An integer; `(value & mask) >> shift`, then `map` keyed by the number as text, or a bool when there is no map. | label or bool |
| `str` | The text as it is. | text |
| `keyed` | A comma list of key and value pairs after an optional header, such as `C1:BSWV WVTP,SINE,FRQ,100HZ,AMP,2V`. The header is the text before the first space when the first token holds a colon. `key` names the pair, looked for only before the `until` token when the reader gives one. A number loses its unit (`100HZ`, `2V`, and `5KHZ` is 5000), text goes through `map`, and a switch reads `ON` or `OFF`. A key the reply lacks leaves the field unknown for that read: a number reads as not-a-number, a label as its table's last label, and a switch is not recorded. | number, label or bool |

## Your own profiles

Set an instrument's Profile to the path of your file, or put files in `profiles/` under the extension's data directory. They load at start with the same checks as the shipped ones, and a mistake stops the start with one sentence that names the file and the key.

Check a file at any time:

```bash
uv run python -m zelos_extension_instruments.profile_load check path/to/my-psu.toml
```

To contribute one, open a pull request with the file and, if you can, a transcript recorded on your unit:

```bash
just record my-psu 192.168.1.40        # identifies, reads every signal, writes tests/transcripts/my-psu/<model>-<firmware>.txt
just record my-psu 192.168.1.40 --set  # also rewrites each level to its current value, to prove the setters
```

`--set` rewrites only values: a voltage, a current, a frequency and the like. It never sends a switch, a label or a command, so it never switches an output, a relay or a trigger.
