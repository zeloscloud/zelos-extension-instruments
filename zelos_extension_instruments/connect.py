"""Reaching an instrument: the address and link settings its config and profile give, the command
it needs before it answers, and its identity."""

from .config import InstrumentConfig
from .link import (
    Address,
    Link,
    LinkError,
    LinkSettings,
    LinkTimeout,
    SerialAddress,
    TcpAddress,
    VisaAddress,
)
from .profile import Profile, is_identity


def address(cfg: InstrumentConfig, profile: Profile) -> Address:
    """Where the config says the instrument is, with the profile's defaults for the rest."""
    if cfg.connection == "demo":
        return TcpAddress(cfg.where, 0)
    if cfg.connection == "lan":
        assert cfg.host is not None
        return TcpAddress(cfg.host, cfg.port or profile.link.lan_port)
    if cfg.connection == "serial":
        assert cfg.serial_port is not None
        return serial_address(cfg.serial_port, profile, cfg.baud)
    assert cfg.resource is not None
    return VisaAddress(cfg.resource, cfg.backend)


def serial_address(port: str, profile: Profile, baud: int | None = None) -> SerialAddress:
    """A serial port framed as the profile says, at `baud` when one is given."""
    link = profile.link
    return SerialAddress(
        port,
        baud=baud or link.baud,
        data_bits=link.data_bits,
        parity=link.parity,
        stop_bits=link.stop_bits,
        flow_control=link.flow_control,
    )


def link_settings(cfg: InstrumentConfig, profile: Profile) -> LinkSettings:
    """The profile's framing and timing, with the config's overrides."""
    link = profile.link
    return LinkSettings(
        write_terminator=cfg.line_ending or link.write_terminator,
        read_terminator=cfg.line_ending or link.read_terminator,
        timeout_s=cfg.timeout_s or link.timeout_s,
        delay_ms=link.delay_ms if cfg.delay_ms is None else cfg.delay_ms,
        command_delay_ms=link.command_delay_ms,
        device_clear=link.device_clear,
    )


def send_first(link: Link, profile: Profile) -> None:
    """The command an instrument needs before it answers anything, with its ack."""
    command = profile.link.first
    assert command is not None
    link.write(command)
    if profile.link.ack is not None:
        reply = link.read()
        if reply.strip() != profile.link.ack:
            raise LinkError(f"{link.where} answered {command} with {reply.strip()!r}")


def ask_identity(link: Link, command: str) -> str:
    """The identity reply, asked again on the same link after a silent or garbled first one: some
    units ignore the first query after a connect, and bytes left from before garble it. Raises
    LinkTimeout when the second is silent too; the caller judges a second reply."""
    try:
        reply = link.query(command)
    except LinkTimeout:
        reply = ""
    if is_identity(reply):
        return reply
    link.clear()
    return link.query(command)


def advice(e: LinkError) -> str:
    """What usually fixes a failed open of this kind."""
    return {
        "refused": "Nothing answers on that port; check the host and port, and that the "
        "instrument's LAN socket is on.",
        "not_found": "No such device; check the port or resource, and that the instrument is "
        "plugged in and on.",
        "unresolved": "The host name cannot be looked up; use its address.",
        "busy": "It is in use by another program; close that program.",
        "held": "Instruments serve one client on their SCPI socket: close the other program "
        "that uses it, or power-cycle the instrument.",
        "permission": "This user may not open it; check the device permissions.",
        "timeout": "It did not answer in time; check the address and the cable.",
        "config": "Fix the setting the message names.",
    }.get(e.kind, "")
