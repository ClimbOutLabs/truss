#!/usr/bin/env python3
"""
One live check from the command line: the acceptance test for the
endpoint checker.

    python3 check_once.py PHOTO "Is the stove off?"
    python3 check_once.py PHOTO "Is the stove off?" --mode multi --yes YES.jpg --no NO.jpg
    python3 check_once.py PHOTO "Is the stove off?" --looks "Knobs up means off."

The endpoint comes from the same environment the server reads
(TRUSS_META_API_KEY for Muse Spark first, TRUSS_BASE_URL, TRUSS_MODEL,
TRUSS_API_KEY, TRUSS_TIMEOUT_S; see endpoint.py), and is routed the
same way, fallback included. Prints the result as JSON; its "model"
names the model that answered.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import endpoint  # noqa: E402
import verdict  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Make one live verdict check.")
    parser.add_argument("photo")
    parser.add_argument("question")
    parser.add_argument("--mode", choices=verdict.MODES, default="single")
    parser.add_argument("--yes", help="the yes-look reference photo")
    parser.add_argument("--no", help="the no-look reference photo")
    parser.add_argument("--looks", help="the looks text (otherwise read from --yes's folder)")
    args = parser.parse_args()

    looks = args.looks
    if looks is None and args.yes:
        looks = verdict.read_looks(Path(args.yes).parent)
    where = endpoint.describe_from_env()
    if where["config_error"]:
        print(where["config_error"], file=sys.stderr)
    line = f"Asking {where['model']} at {where['base_url']}"
    if where["fallback_model"]:
        line += f", falling back to {where['fallback_model']} at {where['fallback_base_url']}"
    print(f"{line}, {args.mode} mode.", file=sys.stderr)
    result = verdict.check(
        args.photo,
        args.question,
        mode=args.mode,
        ref_yes=args.yes,
        ref_no=args.no,
        looks=looks,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
