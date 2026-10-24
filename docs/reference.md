# Instruments reference

Everything the extension records, every setting, every action and every health message. The [README](../README.md) covers what it is and how to start.

## One reading, recorded

A Rigol DP832 on channel 1 answers `:MEAS:ALL? CH1` with `12.0010,0.4998,5.998`. Instruments writes, at the moment the query was sent plus half the round trip:

| Event | What lands |
|---|---|
| `Instruments/psu/ch1` | `voltage` = 12.001 V, `current` = 0.4998 A, `power` = 5.998 W |

Once a second it also reads the setpoints and the state:

| Event | What lands |
|---|---|
| `Instruments/psu/ch1` | `voltage_set` = 12 V, `current_set` = 1 A, `output` = true, `mode` = cv, `protection` = none |
| `Instruments/psu/status` | `rtt_ms` = 18.4, `rate_hz` = 9.9, `errors` = 0, `connected` = true |

## What it records

All events go under one trace source, `Instruments` by default (**Prefix** in Advanced (all instruments)). Each instrument has a name, set by **Name** in the config. Auto-configure names an entry after its model and adds `_1`, `_2` when two would clash; an empty Name uses the address.

| Event | Holds |
|---|---|
| `Instruments/<name>/ch<n>` | The channel's measurements every interval, and its setpoints, output, mode and protection once a second. Only the fields the instrument answers are registered. Field names are the same for every vendor: see [Signals](#signals). A function generator's channel holds its settings once a second and no readings. A data-acquisition unit's channel is named by its address, such as `ch101`: a scan channel holds one reading named after its function, and a relay channel holds `closed`. |
| `Instruments/<name>/<function>` | A multimeter's reading in one signal named after the active function, such as `dc_voltage` or `resistance`, so each keeps its unit. A function's event appears with its first reading. |
| `Instruments/<name>/acquisition` | A scope's acquisition and trigger settings once a second: `timebase` s, `delay` s, `sample_rate` Sa/s, `memory_depth` pts, `points` pts, `acquisitions`, `trigger_status`, `trigger_mode`, `trigger_source`, `trigger_level` V, `trigger_slope`, `trigger_frequency` Hz, `acquisition_mode`, `averages`, `trigger_type`, `pulse_width` s, `pulse_condition`. |
| `Instruments/<name>/ch<n>/waveform` | A scope channel's captured samples, `voltage`. The event appears with the channel's first capture. |
| `Instruments/<name>/identity` | Who the instrument is and how it is reached: `manufacturer`, `model`, `serial`, `firmware`, `profile` and `address`, written at every connect. |
| `Instruments/<name>/status` | `rtt_ms`, the round trip of the last measurement query; `rate_hz`, the readings per second achieved over the last 10 s; `errors`, the instrument errors seen since start; `connected`. |
| `Instruments/<name>/poll` | The extra readings from the config's poll table, one field each. |
| `Instruments/<name>/log` | Connects and disconnects, every set with its source, resets, reboots, self-tests and the other system actions, setpoint changes made at the front panel or by another program, mode and protection changes, the instrument's own errors, replies that fell out of step and came back, and a request dropped because its action stopped waiting. |
| `Instruments/log` | The extension's own log. |

In the Log panel, the **Source** column shows `action` for a set from the app or a notebook, `instrument` for an error the instrument reported, `tx` and `rx` for raw commands, and `instruments` for the extension's notes.

### Signals

| Category | Measured every interval | Read once a second |
|---|---|---|
| Power supply | `voltage` V, `current` A, `power` W | `voltage_set` V, `current_set` A, `ovp_set` V, `ocp_set` A, `ovp_on`, `ocp_on`, `output`, `mode`, `protection`, `remote_sense`, `tracking`, `ovp_delay_set` s |
| Electronic load | `voltage` V, `current` A, `power` W, `resistance` Ohm | `current_set` A, `voltage_set` V, `power_set` W, `resistance_set` Ohm, `slew_set` A/s, `slew_rise_set` A/s, `slew_fall_set` A/s, `current_range`, `voltage_range`, `output` (the input), `mode`, `protection`, `short` |
| AC source | `voltage` V rms, `current` A rms, `power` W, `apparent_power` VA, `power_factor`, `frequency` Hz | `voltage_set` V, `frequency_set` Hz, `current_set` A, `output`, `protection` |
| Multimeter | the reading, as `<function>` under `<name>/<function>` (`dc_voltage`, `resistance`, ...) | `function`, `range`, `autorange`, `nplc` |
| Oscilloscope | per channel, the measurements the scope shows or the ones Measure added: `pkpk`, `maximum`, `minimum`, `amplitude`, `top`, `base`, `mean`, `cycle_mean`, `rms`, `cycle_rms`, `stdev` V, `overshoot`, `preshoot`, `duty` %, `period`, `positive_width`, `negative_width`, `rise_time`, `fall_time` s, `frequency` Hz | per channel: `visible`, `scale` V, `offset` V, `coupling`, `bandwidth_limit`, `probe`; the acquisition under `<name>/acquisition` |
| Function generator | none | per channel: `output`, `shape`, `frequency_set` Hz, `amplitude_set` Vpp, `offset_set` V, `high_set` V, `low_set` V, `phase_set` deg, `duty_set` %, `symmetry_set` %, `width_set` s, `output_load`, `modulation`, `modulation_type`, `modulation_depth_set` %, `modulation_deviation_set` Hz, `phase_deviation_set` deg, `modulation_frequency_set` Hz, `burst`, `burst_cycles_set`, `burst_period_set` s, `sweep`, `sweep_start_set` Hz, `sweep_stop_set` Hz, `sweep_time_set` s |
| Data-acquisition unit | per scan channel, one reading named after its function: `dc_voltage` V, `ac_voltage` V, `dc_current` A, `ac_current` A, `resistance` Ohm, `resistance_4w` Ohm, `frequency` Hz, `period` s, `temperature` degC | per relay channel: `closed` |

`mode` is `off`, `cv`, `cc`, `cp`, `cr`, `unregulated` or `unknown`; for a load it is the configured function, and for a supply it is `off` while the channel's output is off. `protection` is `none`, `ovp`, `ocp`, `opp`, `otp` or `other`. `tracking` is `independent`, `series`, `parallel` or `unknown`, and `current_range` is `low`, `mid`, `high`, `auto` or `unknown`. `voltage_range` takes the same labels as `current_range`. A generator's `shape` is `sine`, `square`, `ramp`, `triangle`, `pulse`, `noise`, `dc`, `arbitrary` or `other`, its `output_load` is `fifty_ohm`, `high_z` or `other`, and its `modulation_type` is `am`, `fm`, `pm`, `fsk`, `pwm` or `other`. The labels of `function` and the scope's settings are listed in [docs/profiles.md](profiles.md#fields-by-category). All show as labels in the app and come back as text from the SDK.

A profile records only what its instrument answers. A generic profile probes the standard queries at start and drops the ones the instrument rejects, with one line in the log.

### Timing

Instruments have no usable clock, so every reading is stamped on the host: the moment the query was sent plus half the round trip. The round trip is `status.rtt_ms`, so the error bound is always in the trace.

**Readings per second** sets how often the measurements are read, 10 by default. The instrument's own turnaround is the floor: ask for more than it answers and it reads back to back, `status.rate_hz` shows what it achieves, and Get State says so. Setpoints, the error queue and the poll table are read on their own intervals, so a slow setpoint pass never costs a measurement. A data-acquisition unit reads one scan of its channels at each reading. A function generator has no readings, only settings read once a second.

## Configuration

The config holds a list of `instruments` and one `advanced` group. Each instrument picks a **Connection**:

| Connection | Reads | Fields |
|---|---|---|
| `lan` (LAN) | A raw SCPI socket | **Host** (from the list, or typed), **Port** (empty uses the profile's default, 5025 for most instruments) |
| `usb` (USB) | A USB-TMC instrument | **Device** (from the list, or a VISA resource string) |
| `serial` (Serial port) | RS-232 or a USB-serial bridge | **Port** (from the list, or a path), **Baud** (empty uses the profile's default) |
| `visa` (VISA resource) | Anything VISA names: `TCPIP0::host::inst0::INSTR`, `TCPIP0::host::hislip0::INSTR`, `GPIB0::5::INSTR` | **Resource** (from the list of what answers VXI-11 or HiSLIP, or typed), **VISA library** |
| `demo` (Demo bench) | A simulated instrument | **Instrument**: supply, load, meter or AC source |

Every entry has a **Profile** (Automatic, a shipped profile, or the path to your own file), a **Name**, and **Readings per second** (10 by default). Its Advanced group holds:

- **Limits**: the most the extension will ever set, whatever the instrument allows.
- **Keep the front panel usable**: on by default.
- **Channels**, the reply timeout, the delay between commands and the line ending: empty uses the profile's values. A data-acquisition unit takes its channels from Scan and Relays instead.
- **Extra readings**: any query whose reply is a number, recorded as its own signal under `<name>/poll`. Each has a name, the query (`{ch}` reads it once per channel), an optional unit, and how often to read it, every 1 s by default.
- **Scan** and **Relays**: for a data-acquisition unit only. See [Data-acquisition units](#data-acquisition-units).

Advanced (all instruments) holds the **Prefix** (`Instruments`) and the **Log level** (`DEBUG`, `INFO`, `WARNING` or `ERROR`, `INFO` by default).

The config is checked when the extension starts. A mistake logs one sentence that names the instrument and the field, then the extension exits.

### Data-acquisition units

A data-acquisition unit has no channels of its own to count, so you list the ones to use. A channel is its address: the slot and the channel number together, such as `101` for channel 1 of slot 1.

| Field | Holds |
|---|---|
| **Scan** (`advanced.scan`) | The channels read at every reading. Each row has a `channel`, a `function` and, for a temperature channel, a `thermocouple`. An optional `nplc` sets the integration time in power line cycles. |
| **Relays** (`advanced.relays`) | The switch channels the Relay action may close and open. |

`function` is `dc_voltage`, `ac_voltage`, `dc_current`, `ac_current`, `resistance`, `resistance_4w`, `frequency`, `period` or `temperature`. `thermocouple` is `B`, `E`, `J`, `K`, `N`, `R`, `S` or `T`, and a temperature channel needs one.

At connect the extension sets the channels up, one command group per function, then the integration times, then the scan list. Each reading then reads one scan and records each channel's value under its function's name. Setup commands change no output.

The config check refuses a channel listed twice, a thermocouple on a channel that does not read temperature, and a channel that is both scanned and a relay. At connect, a channel the profile cannot address, a function it cannot read or an integration time it cannot set stops the connection with a message that names the channel.

```json
{ "instruments": [{ "connection": "lan", "host": "192.168.1.60", "profile": "auto", "name": "logger",
    "advanced": {
      "scan": [{ "channel": 101, "function": "dc_voltage", "nplc": 1 },
               { "channel": 102, "function": "temperature", "thermocouple": "K" }],
      "relays": [201] } }] }
```

### Examples

```json
{ "instruments": [{ "connection": "lan", "host": "192.168.1.40", "profile": "auto", "name": "psu" }] }
```

```json
{ "instruments": [
    { "connection": "usb", "resource": "USB0::0xF4EC::0x1430::SDL13GCX::INSTR", "profile": "siglent-sdl1000x", "name": "load", "rate_hz": 5 },
    { "connection": "serial", "port": "/dev/ttyUSB1", "baud": 9600, "profile": "keysight-e36100", "name": "aux",
      "advanced": { "limits": { "max_voltage": 15, "max_current": 2 } } } ] }
```

```json
{ "instruments": [{ "connection": "demo", "category": "psu" }, { "connection": "demo", "category": "load" }] }
```

### Profiles

A profile is one TOML file that says how to recognise an instrument family, what to read, what can be set and what the replies mean. The shipped ones are listed by **List Profiles** and in [`profiles/`](../profiles/). Each carries an evidence badge:

| Badge | Means |
|---|---|
| `manual` | Transcribed from the programming manual; no unit seen yet. |
| `transcript` | A transcript recorded from a real unit replays against the profile in the tests. |
| `hardware` | Runs on the nightly bench. |

Your own profile loads the same way: set **Profile** to the path of your file, or put files under `profiles/` in the extension's data directory. [docs/profiles.md](profiles.md) is the format, and `just record` writes a transcript from your unit so the profile can earn its badge.

### The VISA library

LAN sockets and serial ports need nothing installed. USB-TMC, VXI-11, HiSLIP and GPIB go through [pyvisa](https://pyvisa.readthedocs.io/): the built-in pure-Python library serves LAN protocols and USB on every platform, and a vendor VISA (NI-VISA, Keysight IO Libraries, R&S VISA) is used automatically for USB and GPIB when one is installed, because on Windows it owns the device. Without a vendor VISA on Windows, bind the instrument to WinUSB once with [Zadig](https://zadig.akeo.ie/). On Linux, give your user access to the device with a udev rule.

## Actions

Every action except List Instruments, List Profiles and Auto-configure takes `instrument`, the instrument's **Name**. Leave it out to act on the first configured instrument. Channel actions take `channel`, from 1. Leave it out for channel 1. A data-acquisition unit's channel is its address, such as 101.

| Action | Path | What it does | Read-only |
|---|---|---|---|
| List Instruments | `Instruments/list_instruments` | Instruments found on the network or on USB, or every serial port, for the address fields. Works while the extension is stopped. | yes |
| List Profiles | `Instruments/list_profiles` | The shipped profiles with their badge, for the Profile field. | yes |
| Auto-configure | `Instruments/auto_config` | Keeps the configured instruments and adds one for each instrument found, or the demo bench when nothing is found and the form is empty. Probes a serial port only when its USB vendor is a known instrument maker. | yes |
| Set | `Instruments/set_values` | Sets any of `voltage`, `current`, `power`, `resistance`, `frequency`, `ovp`, `ocp`, `ovp_delay`, `ovp_on`, `ocp_on`, `remote_sense`, `tracking`, `slew_rise`, `slew_fall`, `current_range`, `voltage_range` and `function` on a channel. Each value is checked against the profile's range and your limits, sent, checked against the instrument's error queue, and read back. `tracking` is `independent`, `series` or `parallel`. The ranges are `low`, `mid`, `high` or `auto`. `function` is a load's mode (`cc`, `cv`, `cp`, `cr`) or a multimeter's function. | no |
| Output | `Instruments/output` | Switches a channel's output, a load's input or a generator's output on or off (`on`, on by default). | no |
| Short | `Instruments/short` | Shorts a load's input, or ends the short (`on`, on by default). | no |
| Set Meter | `Instruments/set_meter` | A multimeter's `function`, `range`, `autorange` and `nplc`. `range` is the largest value to measure, in the function's unit, and the meter picks the range that covers it. | no |
| Set Channel | `Instruments/set_channel` | A scope channel's `scale` (volts per division), `offset`, `coupling`, `probe` factor, `visible` and `bandwidth_limit` (`full`, `20mhz`, `25mhz`, `100mhz`, `200mhz` or `250mhz`). | no |
| Set Timebase | `Instruments/set_timebase` | A scope's `timebase` (seconds per division) and trigger `delay`. | no |
| Set Trigger | `Instruments/set_trigger` | A scope's trigger: `source`, `level`, `slope`, `mode`, `trigger_type` (`edge` or `pulse`), `pulse_width` and `pulse_condition` (`greater` or `less`). | no |
| Set Acquisition | `Instruments/set_acquisition` | A scope's `acquisition_mode` (`normal`, `average`, `peak`, `hires` or `envelope`) and the number of `averages`. | no |
| Run Control | `Instruments/run_control` | Runs, stops, arms a single acquisition, autosets a scope or forces a trigger (`command`: `run`, `stop`, `single`, `autoset` or `force`). | no |
| Measure | `Instruments/measure` | Shows a measurement of a scope channel (`type`, such as `pkpk`) in one of the scope's measurement slots; it is recorded from then on. Reuses the slot that already shows it, else the lowest free one. `slot` picks the slot, and `off` turns it off instead. A scope without slots has none to fill: the extension asks for the measurement itself and records it, and `off` stops recording it. | no |
| Capture Waveform | `Instruments/capture_waveform` | Fetches a scope channel's samples on screen into `<name>/ch<n>/waveform` as one burst, `points` samples spread over the screen (10,000 by default). The trigger instant is placed at the moment of the capture; the samples keep the scope's spacing. | yes |
| Set Waveform | `Instruments/set_waveform` | A generator channel's `shape`, `frequency`, `amplitude` (Vpp), `offset`, `high` and `low` levels, `phase`, `duty`, `symmetry`, pulse `width` and `output_load` (`fifty_ohm` or `high_z`). | no |
| Set Modulation | `Instruments/set_modulation` | A generator channel's modulation: `on`, `type` (`am`, `fm`, `pm`, `fsk` or `pwm`), `depth`, frequency `deviation`, `phase_deviation` and modulation `frequency`. Switching modulation off goes first, and a `type` given with it is only remembered for the next request, because choosing a type switches modulation on in some generators. | no |
| Set Burst | `Instruments/set_burst` | A generator channel's burst: `on`, `cycles` and `period`. | no |
| Set Sweep | `Instruments/set_sweep` | A generator channel's frequency sweep: `on`, `start`, `stop` and `time`. | no |
| Trigger | `Instruments/trigger` | Sends a software trigger to a generator channel. | no |
| Relay | `Instruments/relay` | Closes or opens a relay channel (`closed`, true by default). The channel must be listed under Relays. | no |
| Reset | `Instruments/reset` | Returns the instrument to its reset state: outputs off, settings to defaults. Sends `*RST`, or the family's own reset command, and waits for the instrument to finish. A data-acquisition unit's scan is set up again. No other action resets an instrument. | no |
| Reboot | `Instruments/reboot` | Restarts the instrument. The extension reconnects when it is back. | no |
| Self-Test | `Instruments/self_test` | Runs the instrument's self-test and reports whether it passed. | no |
| Save State | `Instruments/save_state` | Saves the instrument's settings to a memory `slot`, or to a `file`. Give one of the two. A file that already exists is never written over. | no |
| Recall State | `Instruments/recall_state` | Restores the settings from a memory `slot`, or from a `file`. Give one of the two. A data-acquisition unit's scan is set up again. | no |
| Lock Panel | `Instruments/lock_panel` | Locks the instrument's front panel, or unlocks it (`on`, on by default). While it is locked the extension does not hand the panel back, and locks it again each time it puts the instrument back in remote mode. Local and a reconnect end the lock. | no |
| Beep | `Instruments/beep` | Sounds the instrument's beeper. | no |
| Display | `Instruments/display` | Shows `text` on the instrument's display, clears it with empty text, or switches the display on or off (`on`). Text may not contain a double quote or a control character, and it is limited to the length the profile gives. | no |
| Screenshot | `Instruments/screenshot` | Saves a picture of the instrument's screen to `path`. Without a path the file goes under `screenshots` in the Zelos data folder, named `<name>-<UTC date>-<UTC time>`. A relative path is under the data folder. A file that already exists is never written over. | no |
| Read | `Instruments/read` | A fresh measurement of every signal on a channel, now. | yes |
| Get State | `Instruments/get_state` | Health, identity, whether the panel is locked (`panel_locked`), profile with its settings and system functions, channels with their ranges and last values, counters, and the last commands. | yes |
| SCPI | `Instruments/scpi` | Sends any `command` and returns the reply for a query. Logged. Nothing is checked. `timeout_s` is 0.1 to 30 s, 2 s by default. | no |
| Clear | `Instruments/clear` | Clears a channel's latched protection and drains the error queue. The output stays off. | no |
| Local | `Instruments/local` | Gives the front panel back. | no |
| Release | `Instruments/release` | Closes the link so another program can use the instrument. Outputs stay as they are. | no |
| Acquire | `Instruments/acquire` | Takes a released instrument back. | no |

A system action that the profile's family lacks refuses with one sentence, such as `this profile has no reboot`, and sends nothing. Get State lists the system functions a profile gives under `profile.system`.

A set checks every value against the range, the limits and the instrument's digits before anything is sent, so a refusal leaves the instrument as it was. It returns what was sent and what the instrument reads back:

```json
{ "voltage": { "set": 12.0, "readback": 12.0 }, "current": { "set": 2.0, "readback": 2.0 } }
```

A switch or a label comes back as the value the instrument reads back, such as `{ "output": true }` or `{ "tracking": "series" }`. A meter's `range` reads back as the range the meter chose, which may be larger than the one asked for.

A failed action returns one sentence that says why: the range, the limit, a value finer than the instrument's digits, the instrument's own error text, or a readback that disagrees. When a set of several values fails after some went out, the sentence names them. A profile without a readback for a setting says `unverified` in the result instead of passing it as checked.

The other actions return:

| Action | Returns |
|---|---|
| Run Control, Trigger | `{ "ok": true, "command": "run" }` |
| Measure | `{ "slot": 1, "channel": 1, "type": "pkpk", "value": 2.1 }`. With `off`, `{ "slot": 1, "off": true }`. |
| Capture Waveform | The channel, the number of samples, their spacing, the first and last offsets from the trigger, the lowest and highest sample, the volts per division and the seconds the fetch took. |
| Reset | `{ "ok": true, "outputs_off": [1, 2] }`: the channels whose output was on and is now off. A data-acquisition unit adds `relays_opened` and `relays_closed`, the relay addresses that changed. |
| Recall State | `{ "ok": true, "slot": 3, "outputs_on": [], "outputs_off": [1] }`, with `path` in place of `slot` for a file. A data-acquisition unit adds `relays_opened` and `relays_closed`, the relay addresses that changed. |
| Reboot | `{ "ok": true, "back_in_s": 30 }` |
| Self-Test | `{ "passed": true, "reply": "+0", "errors": [] }`. `errors` holds what the instrument queued during the test. |
| Save State | `{ "ok": true, "slot": 3 }`, or `{ "ok": true, "path": "...", "bytes": 1024 }` for a file. |
| Lock Panel | `{ "ok": true, "locked": true }` |
| Display | `{ "ok": true, "on": true, "text": "HELLO" }`, with only the parts you gave. |
| Screenshot | `{ "path": "...", "bytes": 98304, "format": "png" }`. `format` is `png`, `bmp` or `jpg`. |
| Clear | `{ "ok": true, "errors_cleared": 0 }`, without `errors_cleared` when the family has no error queue. |
| SCPI | `{ "reply": "..." }` for a query and `{ "ok": true }` for a command. A block reply returns its size as `block_bytes`, with the bytes as `base64` up to 64 KiB. |
| Beep, Local, Release, Acquire | `{ "ok": true }` |

### From a notebook

```python
from zelos_sdk import connect

with connect() as agent:
    agent.actions.execute(
        "Instruments/set_values",
        {"instrument": "psu", "channel": 1, "voltage": 12.0, "current": 2.0},
    )
    agent.actions.execute("Instruments/output", {"instrument": "psu", "channel": 1, "on": True})
    frame = agent.query("Instruments/psu/ch1.*", start="-1m")
```

### From the command line

```bash
zelos actions execute Instruments/set_values --params '{"instrument": "psu", "channel": 1, "voltage": 12.0}'
zelos actions execute Instruments/read --params '{"instrument": "psu"}'
```

## Reconnects and health

When an instrument stops answering or the link fails, the extension closes it and tries again, from 0.5 s up to every 10 s, until it answers. The gap shows in the trace as a `connected` = false status row. Get State returns one health sentence that says what to do:

| Health | Also know |
|---|---|
| `Connecting to <where>.` | The first open has not finished. |
| `Cannot load the profile <path>: <reason>` | The profile file named in the config has a mistake; the reason names it. |
| `No profile is named <id>. Pick one from the list, or auto.` | The config names a profile that is not shipped. |
| `Cannot open <where>: <reason>. <advice> Retrying.` | The reason comes from the link; the advice names the usual fix: the host and port, the device being plugged in, another program holding it, or a permission. A host that is up but whose SCPI port no longer accepts connections is named as such: most instruments serve one client on that port, and a client that vanished without closing can hold it until the instrument is power-cycled. When the instrument also answers VXI-11, the message gives the VISA resource that reaches it. A family that confirms remote control can answer `the instrument refused remote control`. A data-acquisition unit's message names a configured channel its profile cannot address, read or set, such as `channel 101 is not an address this instrument has`. |
| `Lost <where>: <reason>. Reconnecting.` | Three queries in a row got no reply, or the link broke. |
| `Connected to <maker> <model>. Reading <n> signals and <m> extra readings on <c> channels at <x> Hz (target <y> Hz).` | With a note when the instrument answers slower than the target, and the last instrument error. |
| `Connected to <maker> <model>. Reading <n> settings on <c> channels.` | A function generator has no signals to rate, so the message names its settings and has no rate. |
| `Rebooting; reconnecting in about <n> s.` | Reboot sent the restart command. The extension reconnects after the profile's reboot time, 30 s by default. |
| `Released <where>; run Acquire to take it back.` | Release closed it on purpose. |
| `Stopped.` | The extension stopped this instrument. |

## Limits

- Scopes: profiles cover the Siglent SDS families, the Keysight InfiniiVision 1200 X-Series and the Tektronix 2 Series MSO22 and MSO24. Only the Siglent SDS profile was run, on an SDS824X HD. A scope with measurement slots records what its slots show (the Siglent SDS in Advanced measurement mode); the extension never turns a measurement on by itself, the Measure action does. A scope without slots, such as the Keysight 1200 X-Series and the older Siglent SDS1000X and SDS2000X, records the measurements the Measure action added since the extension started.
- Function generators: the profiles were written from the manuals. The generic profile reads and sets the output, shape, frequency, amplitude and offset only.
- Data-acquisition units: a scan reads one value per listed channel. Three scans in a row that do not read one value per channel reopen the link, which sets the scan up again. The Keithley DAQ6510 profile assumes one reading per channel, which is the default of its reading count. The extension checks the form of an address, not which card is installed.
- A scope's raw socket (port 5025 on a Siglent) serves one client and stays held when a client vanishes; VXI-11 (`TCPIP::<host>::INSTR`) does not. Two programs on the same instrument at once lose replies; the extension is one of them.
- Instruments that speak a reverse-engineered protocol instead of SCPI (Korad and Tenma supplies, among others) have no profile.
- A profile marked `manual` was written from the manual and has not been run against a unit. If yours answers differently, Get State shows the last commands and replies, and a transcript recorded with `just record` makes the fix a test.
- USB-TMC through the built-in library has open issues with some Rigol and Siglent units; use LAN where the instrument has it, or a vendor VISA.
- One instrument is one connection. Instruments that accept a single client refuse a second program while the extension holds them; use Release.
