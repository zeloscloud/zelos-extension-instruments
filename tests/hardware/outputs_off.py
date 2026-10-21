"""Switch every output on the bench off, so a failed run never leaves a supply driving a load."""

import json
import os
import sys

from zelos_extension_instruments import engine
from zelos_extension_instruments.link import LinkError, Settings, open_link
from zelos_extension_instruments.profile import by_id, shipped
from zelos_extension_instruments.record import parse_address


def main() -> int:
    bench = json.loads(os.environ.get("BENCH", "[]"))
    failed = 0
    for entry in bench:
        profile = by_id(shipped(), entry["profile"])
        if profile is None or "output" not in profile.setters:
            continue
        link_defaults = profile.link
        settings = Settings(
            link_defaults.write_terminator,
            link_defaults.read_terminator,
            link_defaults.timeout_s,
            link_defaults.delay_ms,
        )
        try:
            link = open_link(parse_address(entry["address"], profile), settings)
        except LinkError as e:
            print(f"{entry['address']}: {e}")
            failed += 1
            continue
        try:
            selector = engine.Selector(link_defaults.select)
            idn = link.query(profile.match.idn)
            model = idn.split(",")[1].strip() if "," in idn else ""
            setter = profile.setters["output"]
            for ch in range(1, profile.channels_for(model) + 1):
                select = selector.command_for(ch if setter.per_channel else None)
                if select is not None:
                    link.write(select)
                link.write(engine.setter_command(setter, ch=ch, on=False))
            if link_defaults.local:
                link.write(link_defaults.local)
        finally:
            link.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
