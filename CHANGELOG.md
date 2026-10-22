# Changelog

All notable changes to this extension are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the extension uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-22

### Added

- Function generators: Set Waveform, Set Modulation, Set Burst, Set Sweep, Trigger and Output control each channel, and its shape, frequency, levels, output load, modulation, burst and sweep are recorded. Profiles ship for the Keysight 33500B and 33600A, Rigol DG800, DG800 Pro, DG900 Pro and DG1000Z, and Siglent SDG1000X, SDG1000X Plus, SDG2000X and SDG6000X, with a generic one for SCPI generators. Set Modulation switches modulation off before anything else in the request.
- Data-acquisition units: list the channels to scan, each with its function, under Scan, and the switch channels under Relays. Every scan is recorded per channel, and the Relay action closes and opens a relay. Three scans in a row that do not read as set up reopen the link, which sets the scan up again. Profiles ship for the Keysight 34970A, 34972A, DAQ970A, DAQ973A and 34980A and the Keithley DAQ6510.
- System actions: Reset, Reboot, Self-Test, Save State, Recall State, Lock Panel, Beep, Display and Screenshot. Each profile gives its family's commands, and an action the family lacks says so and sends nothing. While Lock Panel is on, the extension does not hand the panel back and locks it again whenever it puts the instrument back in remote mode; Local or a reconnect ends the lock.
- Save State and Recall State take a memory slot or a file, and Save State never writes over an existing file. Reset and Recall State report which outputs went off or on, and on a data-acquisition unit which relays opened or closed, and set its scan up again.
- Screenshot saves the instrument's screen as a PNG, BMP or JPEG file, and never writes over an existing file.
- Supplies: Set takes remote sense, the over-voltage protection delay, the over-voltage and over-current protection switches, and tracking.
- Loads: Set takes the current range, the voltage range, and the rising and falling slew rates. Short shorts a load's input.
- Multimeters: Set Meter sets the function, range, autorange and integration time.
- Scopes: Set Channel takes a bandwidth limit, Set Acquisition sets the acquisition mode and the number of averages, Set Trigger takes a pulse trigger with its width and condition, and Run Control forces a trigger. The acquisition mode, averages, trigger type and pulse settings are recorded.
- Scope profiles for the Keysight InfiniiVision 1200 X-Series, the Tektronix 2 Series (MSO22 and MSO24), and the older Siglent SDS1000X, SDS1000X+, SDS1000X-E, SDS2000X and SDS2000X-E. The Siglent SDS profile gains its system functions and settings.
- A supply profile for the Matrix WPS300S. The other supply, load, meter and AC source profiles gain their family's system functions and the new settings.
- Get State lists the system functions a profile gives and whether the panel is locked.
- Profile format: `[system]`, `[daq]`, keyed replies with an optional `until` token, a `"*"` label for replies a map does not name, values in quoted lists, several error queries, fixed-value ranges, a switch with its own off command, and the connections a profile runs on. Scope profiles may read measurements directly or from a list, and fetch linear waveforms. The format is in `docs/profiles.md`.

### Changed

- `*RST` is sent only when you run Reset, and a saved state is recalled only when you run Recall State. Nothing that changes an output is sent on connect, reconnect, stop or a crash.
- A supply channel with its output off records mode `off`, where it recorded how the supply would regulate.
- `just record --set` rewrites only levels. It never sends a switch, a label or a command.

### Fixed

- A GW Instek GPD supply gets the CR LF line ending it needs.
- A TDK-Lambda Genesys unit on LAN no longer gets the serial-only Genesys profile when Profile is Automatic: a profile can name the connections it runs on.
- Older Siglent SDS1000X and SDS2000X scopes match their own profile instead of the SDS profile, so they get the command set they speak.

## [0.1.0] - 2026-10-22

### Added

- Record DC power supplies, electronic loads, multimeters, AC sources and oscilloscopes over SCPI. Every reading, setpoint, output, mode and protection trip is a signal with the same name on every vendor, and a meter's reading is named after its function.
- Connect over a raw LAN socket, USB-TMC, a serial port or any VISA resource (VXI-11, HiSLIP, GPIB through a vendor VISA), or try the demo bench.
- Auto-configure finds instruments on the network, on USB and on the serial ports of known instrument makers, picks each one's profile from its identity, and names each entry after its model, adding `_1`, `_2` when two would clash.
- Set voltage, current, power, resistance, frequency, protection levels, a load's function and an output from the Actions list. Every value is checked against the instrument's range and your limits before anything is sent, then confirmed by reading it back.
- Switching on a channel of a Rohde & Schwarz NGE100, NGL200, NGM200 or NGP800 also raises its master output, and is refused when that would switch on another channel too.
- Oscilloscopes: the measurements a scope's slots show are recorded per channel, with its channel, timebase and trigger settings. Measure, Capture Waveform, Set Channel, Set Timebase, Set Trigger and Run Control act on it. A Siglent SDS profile ships, recorded from an SDS824X HD.
- Read, SCPI, Get State, Clear, Local, Release and Acquire actions, and Extra readings for any query that answers a number.
- Each instrument's identity, connection, reading rate and errors are recorded beside its readings, and Get State says what to do when it cannot connect.
- A profile per instrument family, each checked when it loads; your own profile files load the same way. The format is in `docs/profiles.md`.
- Safe on a live bench: outputs are never touched on connect, reconnect, stop or crash, `*RST` is never sent, and the front panel is handed back when the extension stops.
- A late reply never lands under the next query: the extension resynchronizes after a silent or garbled reply, and reconnects with a backoff when the link fails.
