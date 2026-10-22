# Changelog

All notable changes to this extension are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the extension uses [Semantic Versioning](https://semver.org/).

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
