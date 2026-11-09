"""pi-panel-immich's command line.

    pi-panel-immich --config config.toml          the slideshow
    pi-panel-immich --check-config config.toml    validate it and exit (the manifest's [config] check)
    pi-panel-immich --convert-yaml OLD.yaml NEW.toml
                                                  migrate a config.yaml (needs PyYAML:
                                                  uv run --with pyyaml pi-panel-immich --convert-yaml ...)

Only the slideshow imports pygame and SDL; the checks need neither, nor a
display or the network.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, convert_yaml, load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pi-panel-immich",
                                description="Pi Panel Immich: an Immich photo frame")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", "-c", type=Path, help="config.toml to run with")
    mode.add_argument("--check-config", type=Path, metavar="PATH",
                      help="validate a config.toml and exit: 0 if good, 1 with the reason if not")
    mode.add_argument("--convert-yaml", nargs=2, type=Path, metavar=("OLD", "NEW"),
                      help="write an old config.yaml's settings as config.toml")
    args = p.parse_args(argv)

    if args.convert_yaml:
        src, dest = args.convert_yaml
        try:
            convert_yaml(src, dest)
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {dest} from {src}")
        return 0

    path = args.check_config or args.config
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1 if args.check_config else 2
    if args.check_config:
        print(f"config ok: {path}")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from .__main__ import run     # pygame and SDL from here on
    run(path, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
