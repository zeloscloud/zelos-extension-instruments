# Changelog

All notable changes to this extension are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the extension uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added

- Record DC power supplies, electronic loads, multimeters and AC sources over SCPI: every measured value, setpoint, output state, mode and protection trip becomes a signal with the same name on every vendor.
- Set voltage, current, power, resistance, frequency, protection levels, a load's function and an output from the Action panel, a notebook or Zelos AI, each checked against the instrument's range and your limits and confirmed by the readback.
- Connections: a raw LAN socket, USB-TMC, a serial port, any VISA resource (VXI-11, HiSLIP, GPIB through a vendor VISA), and a demo bench.
- Auto-configure finds instruments by mDNS, VXI-11 broadcast, USB and the serial ports of known instrument vendors, and picks the profile from each one's identity.
- Instrument knowledge in profile files, one per family, checked at start; your own profile files load the same way.
- A raw SCPI action, a poll table for any extra reading, Release and Acquire, Local, Clear and Get State.
- A transcript recorder and a fake instrument served from any profile, for tests and the demo.
- Get State names a SCPI port that another client holds, and gives the VISA resource when the instrument also answers VXI-11; the VISA resource list offers what answers VXI-11 or HiSLIP.
- Oscilloscopes: a `scope` category, a Siglent SDS profile recorded from an SDS824X HD, the measurements a scope shows as per-channel signals, its channel, timebase and trigger settings as state, and the Set Channel, Set Timebase, Set Trigger, Run Control, Measure and Capture Waveform actions.
- Protection enables, remote sense and tracking on supplies; slew rate and current range on loads; range, auto-range and integration time on meters; a meter's function read with every reading.
- A late reply never lands under the next query: the worker resynchronizes after any silent query, and only the identity ends a resynchronization. The error queue is drained at connect and before every request, and every entry a request raises is reported. A value finer than the instrument's digits is refused instead of rounded. The SCPI action waits its own timeout and returns a block reply as base64.
- A USB-TMC device this user cannot read is listed with the driver or permission fix for the platform.
