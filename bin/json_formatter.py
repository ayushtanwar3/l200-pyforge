#!/usr/bin/env python3
import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate, sort keys, and pretty-print JSON from --input or stdin."
    )
    parser.add_argument("--input", type=str, default=None, help="Raw JSON string")
    parser.add_argument("--indent", type=int, default=2, help="Indentation spaces")
    args = parser.parse_args()

    raw = args.input if args.input is not None else sys.stdin.read()
    if not raw or not raw.strip():
        sys.stderr.write("Error: Empty JSON input.\n")
        return 1

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"Invalid JSON: {exc}\n")
        return 1

    print(json.dumps(parsed, sort_keys=True, indent=args.indent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
