"""Profile readers, flags and setters: the signal tables of a profile, checked and built."""

from typing import Any

from .profile import (
    BOOL_FIELDS,
    COMMAND_SETTERS,
    ENUM_FIELDS,
    FUNCTION_LABELS,
    LABEL_SETTERS,
    MAP_ANY,
    MODE_LABELS,
    PARSES,
    PROTECTION_LABELS,
    RANGE_KEYS,
    SETTERS,
    SWITCH_SETTERS,
    VALUE_SETTERS,
    Flag,
    Master,
    Range,
    RangeOverride,
    Reader,
    Setter,
    readback_field,
)
from .profile_check import Check, section_keys


def _parse_options(
    c: Check, raw: dict[str, Any], where: str, labels: tuple[str, ...] | None
) -> dict[str, Any]:
    """The parser settings shared by readers and flags, checked."""
    parse = c.choice(raw, "parse", PARSES, "float", where)
    mapping = c.text_table(raw, "map", where, "text to label")
    if parse == "map" and not mapping:
        c.fail(f"{where}.map", 'missing: parse = "map" needs it.')
    if parse == "bool" and any(v not in ("on", "off") for v in mapping.values()):
        c.fail(f"{where}.map", 'must map each word to "on" or "off" for parse = "bool".')
    if labels is not None:
        c.one_of(mapping.values(), labels, f"{where}.map")
    mask = _per_channel_ints(c, raw, "mask", where)
    if parse == "bits" and not any(mask):
        c.fail(f"{where}.mask", 'missing: parse = "bits" needs it.')
    shift = _per_channel_ints(c, raw, "shift", where)
    if len(mask) > 1 and len(shift) > 1 and len(mask) != len(shift):
        c.fail(where, "mask and shift lists must have one entry per channel.")
    index = raw.get("index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0):
        c.fail(f"{where}.index", "must be a whole number from 0.")
        index = None
    words: dict[str, str] = {}
    for key, label in mapping.items():
        if key.strip() != MAP_ANY:
            words.setdefault(label, key.strip())
    scale = c.number(raw, "scale", 1.0, where) or 1.0
    if scale == 0 or (scale != 1.0 and parse not in ("float", "floats", "keyed")):
        c.fail(
            f"{where}.scale", 'must be a non-zero number, for parse = "float", "floats" or "keyed".'
        )
        scale = 1.0
    key = c.string(raw, "key", None, where)
    if parse == "keyed" and key is None:
        c.fail(f"{where}.key", 'missing: parse = "keyed" needs it.')
    if parse != "keyed" and key is not None:
        c.fail(f"{where}.key", 'is only for parse = "keyed".')
    until = c.string(raw, "until", None, where)
    if parse != "keyed" and until is not None:
        c.fail(f"{where}.until", 'is only for parse = "keyed".')
    return {
        "parse": parse,
        "map": {k.strip().strip('"').casefold(): v for k, v in mapping.items()},
        "words": words,
        "mask": mask,
        "shift": shift,
        "hex": c.choice(raw, "format", ("int", "hex"), "int", where) == "hex",
        "index": index,
        "scale": scale,
        "key": key,
        "until": until,
    }


def _per_channel_ints(c: Check, raw: dict[str, Any], key: str, where: str) -> tuple[int, ...]:
    """A whole number, or a list with one per channel."""
    value = raw.get(key, 0)
    values = value if isinstance(value, list) else [value]
    if not values or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in values):
        c.fail(f"{where}.{key}", "must be a whole number, or a list with one per channel.")
        return (0,)
    return tuple(values)


def load_readers(
    c: Check, raw: dict[str, Any], section: str, units: dict[str, str | None]
) -> tuple[Reader, ...]:
    readers: list[Reader] = []
    for key, entry in raw.items():
        where = f"{section}.{key}"
        entry = c.table(entry, where)
        c.unknown(
            entry,
            (
                "query",
                "parse",
                "fields",
                "index",
                "map",
                "mask",
                "shift",
                "format",
                "flags",
                "optional",
                "channel",
                "scale",
                "key",
                "until",
            ),
            where,
        )
        if "flags" in entry:
            if key != "protection":
                c.fail(where, "flags are only for protection.")
            flags = _flags(c, entry.get("flags"), where)
            readers.append(
                Reader(
                    key,
                    None,
                    "map",
                    (key,),
                    None,
                    {},
                    {},
                    (0,),
                    (0,),
                    False,
                    c.boolean(entry, "optional", False, where),
                    c.boolean(entry, "channel", True, where),
                    flags,
                )
            )
            continue
        query = c.required(entry, "query", where)
        c.template(query, {"ch"}, f"{where}.query")
        fields_raw = entry.get("fields")
        labels = ENUM_FIELDS.get(key)
        options = _parse_options(c, entry, where, labels)
        if fields_raw is None:
            fields = (key,)
            if options["parse"] == "floats":
                c.fail(f"{where}.fields", 'missing: parse = "floats" needs it.')
        else:
            if not isinstance(fields_raw, list) or not all(isinstance(f, str) for f in fields_raw):
                c.fail(f"{where}.fields", "must be a list of field names.")
                fields_raw = []
            if options["parse"] != "floats":
                c.fail(f"{where}.fields", 'only parse = "floats" fills several fields.')
            fields = tuple(fields_raw)
        keyed = options["parse"] == "keyed"
        for name in fields:
            if name not in units:
                c.fail(
                    where, f"{name} is not a {section} field of this category: {', '.join(units)}."
                )
            if name in ENUM_FIELDS and options["parse"] not in ("map", "bits", "keyed"):
                c.fail(where, f'{name} needs parse = "map", "bits" or "keyed".')
            if name in ENUM_FIELDS and keyed and not options["map"]:
                c.fail(f"{where}.map", f"missing: keyed {name} needs a map of reply word to label.")
            if name in BOOL_FIELDS and options["parse"] not in ("bool", "bits", "keyed"):
                c.fail(where, f'{name} needs parse = "bool", "bits" or "keyed".')
            if (
                name in BOOL_FIELDS
                and keyed
                and any(v not in ("on", "off") for v in options["map"].values())
            ):
                c.fail(f"{where}.map", 'must map each word to "on" or "off" for a switch.')
            if (
                name not in ENUM_FIELDS
                and name not in BOOL_FIELDS
                and options["parse"] not in ("float", "floats", "int", "keyed")
            ):
                c.fail(
                    where, f'{name} is a number; use parse = "float", "floats", "int" or "keyed".'
                )
        readers.append(
            Reader(
                name=key,
                query=query,
                parse=options["parse"],
                fields=fields,
                index=options["index"],
                map=options["map"],
                words=options["words"],
                mask=options["mask"],
                shift=options["shift"],
                hex=options["hex"],
                optional=c.boolean(entry, "optional", False, where),
                per_channel=c.boolean(entry, "channel", True, where),
                flags=(),
                scale=options["scale"],
                key=options["key"],
                until=options["until"],
            )
        )
    return tuple(readers)


def _flags(c: Check, raw: Any, where: str) -> tuple[Flag, ...]:
    if not isinstance(raw, list) or not raw:
        c.fail(f"{where}.flags", "must be a non-empty list of tables.")
        return ()
    flags: list[Flag] = []
    for i, entry in enumerate(raw):
        at = f"{where}.flags[{i}]"
        entry = c.table(entry, at)
        c.unknown(entry, ("query", "parse", "value", "map", "mask", "shift", "format", "index"), at)
        query = c.required(entry, "query", at)
        c.template(query, {"ch"}, f"{at}.query")
        value = c.choice(entry, "value", PROTECTION_LABELS[1:], "other", at)
        options = _parse_options(c, entry, at, None)
        if options["parse"] not in ("bool", "bits"):
            c.fail(f"{at}.parse", "must be bool or bits.")
        flags.append(
            Flag(
                query,
                options["parse"],
                value,
                options["map"],
                options["mask"],
                options["shift"],
                options["hex"],
                options["index"],
            )
        )
    return tuple(flags)


def load_setters(
    c: Check,
    raw: dict[str, Any],
    category: str,
    ranges: dict[str, Range],
    overrides: tuple[RangeOverride, ...],
) -> dict[str, Setter]:
    setters: dict[str, Setter] = {}
    allowed = SETTERS[category]
    for key, entry in raw.items():
        where = f"setters.{key}"
        if key not in allowed:
            c.fail(where, f"is not a setter of this category: {', '.join(allowed)}.")
            continue
        entry = c.table(entry, where)
        c.unknown(
            entry,
            ("command", "off", "verify", "range", "map", "channel", "scale", "master"),
            where,
        )
        per_channel = c.boolean(entry, "channel", True, where)
        if key in COMMAND_SETTERS:
            commands = c.commands(entry, "command", where, required=True)
            command = commands[0] if commands else ""
        else:
            command = c.required(entry, "command", where)
            commands = (command,)
        # A scope's trigger setters name the trigger source; a generator's the modulation type.
        extra = {"src"} if category == "scope" else {"modulation"} if category == "fgen" else set()
        if category == "dmm" and key in ("range", "autorange", "nplc"):
            extra = {"function"}
        verify = c.string(entry, "verify", None, where)
        if verify is not None:
            c.template(verify, {"ch"} | extra, f"{where}.verify")
        mapping = c.text_table(entry, "map", where, "label to instrument word")
        if "function" in extra:
            # The root word that fills {function}, by function label.
            c.one_of(mapping, FUNCTION_LABELS, f"{where}.map")
        off = c.string(entry, "off", None, where)
        if off is not None and key not in SWITCH_SETTERS:
            c.fail(f"{where}.off", "is only for a switch setter.")
        if key in VALUE_SETTERS:
            c.template(command, {"ch", "value"} | extra, f"{where}.command")
            if "{value}" not in command:
                c.fail(f"{where}.command", "must contain {value}.")
        elif key in SWITCH_SETTERS:
            c.template(command, {"ch", "on", "bit"} | extra, f"{where}.command")
            if off is not None:
                c.template(off, {"ch", "on", "bit"} | extra, f"{where}.off")
            elif "{on}" not in command and "{bit}" not in command:
                c.fail(f"{where}.command", "must contain {on} or {bit}, or come with off.")
        elif key in LABEL_SETTERS:
            c.template(command, {"ch", "value"} | extra, f"{where}.command")
            if "{value}" not in command:
                c.fail(f"{where}.command", "must contain {value}, the instrument's word.")
            labels = ENUM_FIELDS.get(readback_field(category, key), ())
            if not mapping:
                c.fail(f"{where}.map", f"missing: {key} needs a map of label to instrument word.")
            c.one_of(mapping, labels, f"{where}.map")
        elif key == "function":
            c.template(command, {"ch", "function"}, f"{where}.command")
            if "{function}" not in command:
                c.fail(f"{where}.command", "must contain {function}.")
            labels = MODE_LABELS if category == "load" else FUNCTION_LABELS
            if not mapping:
                c.fail(f"{where}.map", "missing: function needs a map of label to instrument word.")
            c.one_of(mapping, labels, f"{where}.map")
        else:
            # An instrument-wide command has no channel to fill in.
            for one in commands:
                c.template(one, ({"ch"} if per_channel else set()) | extra, f"{where}.command")
        range_key = c.string(entry, "range", None, where)
        if range_key is not None and range_key not in RANGE_KEYS:
            c.fail(f"{where}.range", f"must be one of {', '.join(RANGE_KEYS)}.")
        scale = c.number(entry, "scale", 1.0, where) or 1.0
        if scale == 0 or (scale != 1.0 and key not in VALUE_SETTERS):
            c.fail(f"{where}.scale", "must be a non-zero number, on a value setter.")
            scale = 1.0
        setters[key] = Setter(
            name=key,
            command=command,
            commands=commands,
            verify=verify,
            range=range_key,
            map=mapping,
            per_channel=per_channel,
            scale=scale,
            master=_master(c, entry, key, where),
            off=off,
        )
    return setters


def _master(c: Check, entry: dict[str, Any], key: str, where: str) -> Master | None:
    raw = entry.get("master")
    if raw is None:
        return None
    where = f"{where}.master"
    if key != "output":
        c.fail(where, "is only for the output setter.")
        return None
    raw = c.table(raw, where)
    c.unknown(raw, section_keys(Master), where)
    command = c.required(raw, "command", where)
    c.template(command, {"on", "bit"}, f"{where}.command")
    if "{on}" not in command and "{bit}" not in command:
        c.fail(f"{where}.command", "must contain {on} or {bit}.")
    query = c.required(raw, "query", where)
    c.template(query, set(), f"{where}.query")
    armed = c.required(raw, "armed", where)
    c.template(armed, {"ch"}, f"{where}.armed")
    return Master(command, query, armed)
