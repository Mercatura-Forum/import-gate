"""The command line: `import-gate PATH [--changed f …] [--json] [--facts]`.

Exit codes: 0 = every import resolves, 1 = rejections, 2 = usage error.
A tree the extractor cannot read exits 0 with `method: unavailable` on the
report — the gate fails open loudly, never silently closed (see gate.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# explicit submodule imports: the package re-exports `gate` the FUNCTION,
# which shadows the submodule as a package attribute
from .contract import facts as _facts
from .gate import gate as _gate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="import-gate",
        description="Deterministic import validation for generated TypeScript "
                    "— catch TS2307 before the build runs.",
    )
    ap.add_argument("root", type=Path,
                    help="package root: the directory holding src/ (and "
                         "package.json, tsconfig.json)")
    ap.add_argument("--changed", nargs="*", default=None, metavar="FILE",
                    help="restrict the check to these changed files "
                         "(default: the whole tree)")
    ap.add_argument("--json", action="store_true",
                    help="print the full report as JSON")
    ap.add_argument("--facts", action="store_true",
                    help="print the page-router contract as prompt-ready "
                         "text and exit")
    args = ap.parse_args(argv)

    if not args.root.is_dir():
        print(f"import-gate: not a directory: {args.root}", file=sys.stderr)
        return 2

    if args.facts:
        print(_facts(args.root), end="")
        return 0

    rep = _gate(args.root, changed=args.changed)
    if args.json:
        print(json.dumps(rep.as_record(), indent=1))
    elif rep.ok:
        n = rep.examined or rep.modules
        note = "" if rep.method == "parser" else f" [{rep.method}]"
        err = f", {rep.errored} unreadable" if rep.errored else ""
        print(f"import-gate: ok — {n} module(s) examined{err}{note}")
    else:
        print(rep.gate_detail())
        print()
        print(rep.retry_text())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
