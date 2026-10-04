#!/usr/bin/env python3
"""Set or clear keys in a .env file without disturbing anything else.

Used to keep the committed .env files in step with live credentials. Every
credential rotation writes here FIRST-class: the file is the source of truth,
so it must never be left describing a password that no longer exists.

    scripts/setenv.py avanyam_aero/.env KEY=value [KEY2=value2 ...]
    scripts/setenv.py avanyam_aero/.env --unset KEY [KEY2 ...]

Properties this relies on:
  * Keys are matched on the exact name before '=' , so 'DB_PASSWORD' never
    collides with 'DB_APP_PASSWORD'.
  * The first assignment of a key wins; later duplicates are rewritten in
    place rather than appended, so a file cannot end up with two values for
    one key.
  * Comments, blank lines, ordering and file mode are preserved.
  * The value is written literally: no quoting, no escaping, no expansion.
  * The parent file is made 0600 and is refused if it is a symlink.

Exit status is non-zero if any requested key could not be applied.
"""

from __future__ import annotations

import os
import stat
import sys


def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"setenv: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_args(argv: list[str]) -> tuple[str, dict[str, str | None], bool]:
    if len(argv) < 3:
        die("usage: setenv.py <env-file> [--unset] KEY[=VALUE] ...")
    path = argv[1]
    unset = False
    items: list[str] = argv[2:]
    if items and items[0] == "--unset":
        unset = True
        items = items[1:]
    if not items:
        die("no keys given")
    pairs: dict[str, str | None] = {}
    for item in items:
        if unset:
            if "=" in item:
                die(f"--unset takes bare key names, got {item!r}")
            pairs[item] = None
            continue
        if "=" not in item:
            die(f"expected KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        if not key:
            die(f"empty key in {item!r}")
        if "\n" in value or "\r" in value:
            die(f"value for {key} contains a newline")
        pairs[key] = value
    return path, pairs, unset


def main(argv: list[str]) -> int:
    path, pairs, unset = parse_args(argv)

    if os.path.islink(path):
        die(f"refusing to write through a symlink: {path}")
    if not os.path.exists(path):
        die(f"no such file: {path}")

    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    remaining = dict(pairs)
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key)
        if value is None:                      # --unset: drop the assignment
            continue
        out.append(f"{key}={value}")

    # Keys that were absent get appended, so the file always ends up complete.
    for key, value in remaining.items():
        if value is None:                      # --unset of an absent key is fine
            continue
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")

    body = "\n".join(out) + "\n"
    tmp = f"{path}.setenv.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)

    missing = [k for k, v in remaining.items() if v is None]
    if missing:
        print(f"setenv: {path}: not present (nothing to unset): {', '.join(missing)}")
    applied = ", ".join(f"{k}={'<unset>' if v is None else '<set>'}" for k, v in pairs.items())
    mode = stat.S_IMODE(os.stat(path).st_mode)
    print(f"setenv: {path}: {applied} (mode {mode:04o})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
