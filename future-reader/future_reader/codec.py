"""Bounded, unambiguous JSON and portable content digests."""

import hashlib
import json
from pathlib import Path

MAX_JSON = 4 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def loads(raw, *, limit=MAX_JSON):
    if len(raw) > limit:
        raise ValueError("JSON exceeds the byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON contains duplicate keys")
            result[key] = value
        return result

    def bad_constant(_):
        raise ValueError("JSON numbers must be finite")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
        canonical(value)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("JSON encoding or nesting exceeds the supported scope") from error
    return value


def read_json(path):
    with Path(path).open("rb") as stream:
        return loads(stream.read(MAX_JSON + 1))
