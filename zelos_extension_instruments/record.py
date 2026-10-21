"""Record a transcript from a real instrument, so the tests can replay what it said.

    python -m zelos_extension_instruments.record <profile-id> <address> [--channels N] [--set]

The address is `host[:port]` for a raw socket, a serial port path (`COM7`, `/dev/ttyUSB0`), or a
VISA resource string. The recorder identifies the instrument and reads every signal and state
query of the profile, plus its range and error queries. It writes nothing to the instrument
unless `--set` is given, which rewrites each setpoint to its current value to prove the setter
commands. The transcript lands in `tests/transcripts/<profile>/<model>-<firmware>.txt`.
"""

import argparse
import re
import sys
from pathlib import Path

from . import engine
from .link import (
    Link,
    LinkError,
    SerialAddress,
    Settings,
    TcpAddress,
    VisaAddress,
    open_link,
)
from .profile import Profile, by_id, load_file, parse_idn, shipped

TRANSCRIPTS = Path(__file__).resolve().parent.parent / "tests" / "transcripts"
_HOST_PORT = re.compile(r"^(?P<host>[^:]+|\[[^\]]+\])(?::(?P<port>\d+))?$")


def parse_address(text: str, profile: Profile, baud: int | None = None):
    """A command-line address as a link address."""
    if "::" in text:
        return VisaAddress(text)
    if text.upper().startswith("COM") or text.startswith("/dev/"):
        link = profile.link
        return SerialAddress(
            text,
            baud=baud or link.baud,
            data_bits=link.data_bits,
            parity=link.parity,  # type: ignore[arg-type]
            stop_bits=link.stop_bits,
            flow_control=link.flow_control,  # type: ignore[arg-type]
        )
    found = _HOST_PORT.match(text)
    if found is None:
        raise ValueError(f"not an address: {text}")
    port = int(found.group("port")) if found.group("port") else profile.link.lan_port
    return TcpAddress(found.group("host").strip("[]"), port)


def load_transcript(path: Path) -> list[tuple[str, str | None]]:
    """`> command` and `< reply` lines as pairs; a command with no `<` has reply None."""
    pairs: list[tuple[str, str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith("> "):
            pairs.append((line[2:], None))
        elif line.startswith("< ") and pairs and pairs[-1][1] is None:
            pairs[-1] = (pairs[-1][0], line[2:])
    return pairs


class Recorder:
    """Runs the profile's queries against a link and keeps what came back."""

    def __init__(self, link: Link, profile: Profile) -> None:
        self._link = link
        self._profile = profile
        self._selector = engine.Selector(profile.link.select)
        self.lines: list[str] = []
        self.failures: list[str] = []

    def query(self, command: str) -> str | None:
        self.lines.append(f"> {command}")
        try:
            reply = self._link.query(command)
        except LinkError as e:
            self.lines.append(f"# {e}")
            self.failures.append(f"{command}: {e}")
            return None
        self.lines.append(f"< {reply}")
        return reply

    def send(self, command: str) -> None:
        self.lines.append(f"> {command}")
        ack = self._profile.link.ack
        if ack is None:
            self._link.write(command)
            return
        reply = self._link.query(command)
        self.lines.append(f"< {reply}")

    def select(self, ch: int | None) -> None:
        command = self._selector.command_for(ch)
        if command is not None:
            self.send(command)

    def run(self, channels: int, allow_set: bool) -> str:
        profile = self._profile
        reply = self.query(profile.match.idn)
        idn = parse_idn(reply) if reply else None
        if profile.link.remote:
            self.send(profile.link.remote)
        self.lines.append("# ranges")
        model = idn.model if idn else ""
        for ch in range(1, channels + 1):
            for key in profile.ranges:
                r = profile.range_for(key, model, ch)
                if r is None:
                    continue
                for template in (r.min_query, r.max_query):
                    if template is not None:
                        self.select(ch)
                        self.query(engine.render(template, ch=ch))
        self.lines.append("# signals and state")
        plan = engine.compile_plan(profile, channels)
        for step in plan:
            self.select(step.channel)
            reply = self.query(step.query)
            if reply is None:
                continue
            try:
                if step.flag is not None:
                    engine.flag_is_set(step.flag, reply, step.channel)
                else:
                    engine.parse_reply(step.reader, reply, step.channel)
            except engine.ParseError as e:
                self.lines.append(f"# cannot parse: {e}")
                self.failures.append(f"{step.query}: {e}")
        if profile.errors is not None:
            self.lines.append("# error queue")
            for _ in range(profile.errors.drain):
                reply = self.query(profile.errors.query)
                if reply is None or engine.read_error(profile.errors, reply) is None:
                    break
        if allow_set:
            self.lines.append("# setters, each rewritten to its current value")
            for name, setter in profile.setters.items():
                verify = profile.verify_query(setter)
                if verify is None or name in ("output", "function", "clear"):
                    continue
                for ch in range(1, channels + 1):
                    self.select(ch if setter.per_channel else None)
                    current = self.query(engine.render(verify, ch=ch))
                    if current is None:
                        continue
                    try:
                        value = engine.parse_float(current)
                    except engine.ParseError:
                        continue
                    key = profile.range_key(setter)
                    r = profile.range_for(key, model, ch) if key else None
                    self.send(engine.setter_command(setter, ch=ch, value=value, range_=r))
                    if profile.errors is not None:
                        self.query(profile.errors.query)
                    self.query(engine.render(verify, ch=ch))
        if profile.link.local:
            self.send(profile.link.local)
        name = f"{idn.model}-{idn.firmware}" if idn else "unknown"
        return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("profile", help="a shipped profile id, or a path to a profile file")
    parser.add_argument("address", help="host[:port], a serial port, or a VISA resource")
    parser.add_argument("--channels", type=int, default=None)
    parser.add_argument("--baud", type=int, default=None)
    parser.add_argument("--set", action="store_true", help="also prove the setters")
    parser.add_argument("--out", type=Path, default=None, help="write here instead")
    args = parser.parse_args(argv)
    profile = (
        load_file(args.profile)
        if args.profile.endswith(".toml")
        else by_id(shipped(), args.profile)
    )
    if profile is None:
        print(f"no profile is named {args.profile}")
        return 2
    address = parse_address(args.address, profile, args.baud)
    settings = Settings(
        profile.link.write_terminator,
        profile.link.read_terminator,
        profile.link.timeout_s,
        profile.link.delay_ms,
    )
    try:
        link = open_link(address, settings)
    except LinkError as e:
        print(f"cannot open {address.where}: {e}")
        return 1
    recorder = Recorder(link, profile)
    try:
        idn_reply = link.query(profile.match.idn)
    except LinkError as e:
        print(f"{address.where} did not answer {profile.match.idn}: {e}")
        link.close()
        return 1
    idn = parse_idn(idn_reply)
    channels = args.channels or profile.channels_for(idn.model)
    try:
        name = recorder.run(channels, args.set)
    finally:
        link.close()
    out = args.out or TRANSCRIPTS / profile.id / f"{name}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = [
        f"# profile: {profile.id}",
        f"# identity: {idn.text}",
        f"# channels: {channels}",
        f"# address: {address.where}",
    ]
    out.write_text("\n".join(header + recorder.lines) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    for failure in recorder.failures:
        print(f"  problem: {failure}")
    return 1 if recorder.failures else 0


if __name__ == "__main__":
    sys.exit(main())
