"""Serve a fake instrument from a profile until Ctrl-C.

python -m zelos_extension_instruments.sim rigol-dp800 --model DP832
"""

import argparse
import time
from pathlib import Path

from ..profile import Profile
from ..profile_load import SHIPPED_DIR, ProfileError, load_file
from .fake import FakeInstrument
from .server import FakeServer


def _profile(name: str) -> Profile:
    if Path(name).is_file():
        return load_file(name)
    # A profile's id is its file name, so one broken neighbor cannot stop this one loading.
    found = sorted(SHIPPED_DIR.rglob(f"{name}.toml"))
    if not found:
        raise ValueError(f"no shipped profile has the id {name}, and no file has that path")
    return load_file(found[0])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m zelos_extension_instruments.sim",
        description="Serve a fake instrument from a profile over TCP until Ctrl-C.",
    )
    parser.add_argument("profile", help="a shipped profile id, or the path to a profile file")
    parser.add_argument("--port", type=int, default=None, help="default: the profile's LAN port")
    parser.add_argument("--model", help="the model to fake; default: one the profile matches")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    try:
        profile = _profile(args.profile)
        instrument = FakeInstrument(profile, args.model)
    except (ProfileError, ValueError) as e:
        parser.error(str(e))
    port = profile.link.lan_port if args.port is None else args.port
    with FakeServer(instrument, host=args.host, port=port) as server:
        print(f"listening on {args.host}:{server.port}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
