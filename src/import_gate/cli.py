"""The command line: `import-gate PATH [--changed f …] [--format …] [--facts]`.

Exit codes: 0 = every import resolves, 1 = rejections, 2 = usage error.
A tree the extractor cannot read exits 0 with `method: unavailable` on the
report — the gate fails open loudly, never silently closed (see gate.py).

Formats: `text` (the default human report), `json` (the full record;
`--json` is the older spelling and stays valid), and `github` — one
workflow-command annotation per finding, so a CI failure lands on the
offending line of the pull-request diff. In `github` mode the root path is
used exactly as the caller wrote it: the runner matches `file=` against
workspace-relative paths, and a CI step passes a workspace-relative root.
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


def _esc(text: str, *, prop: bool = False) -> str:
    """Escape per the runner's workflow-command rules. An unescaped newline
    truncates the annotation and swallows everything printed after it."""
    out = str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        out = out.replace(":", "%3A").replace(",", "%2C")
    return out


def _github_annotation(level: str, file: str, line: int, title: str,
                       message: str) -> str:
    return (f"::{level} file={_esc(file, prop=True)},line={int(line) or 1},"
            f"title={_esc(title, prop=True)}::{_esc(message)}")


def _print_github(rep, root: Path) -> None:
    """Annotations first (one per line, nothing else on those lines), then
    the same human summary the text format prints, for the raw log."""
    base = str(root).rstrip("/")
    src = "src" if base in ("", ".") else f"{base}/src"
    for r in rep.rejections:
        print(_github_annotation(
            "error", f"{src}/{r.importer}", r.line, r.ts_code,
            f"'{r.spec}' cannot resolve ({r.kind}): {r.detail}"))
    for a in rep.advisories:
        print(_github_annotation(
            "warning", f"{src}/{a.get('importer', '')}", a.get("line") or 1,
            str(a.get("ts_code") or "export-shape"),
            f"'{a.get('target', '')}' ({a.get('kind', '')}): {a.get('detail', '')}"))
    if rep.method != "parser" or rep.errored:
        err = f", {rep.errored} unreadable" if rep.errored else ""
        print(f"::notice ::import-gate ran via {_esc(rep.method)}{_esc(err)}")
    if rep.ok:
        print(f"import-gate: ok — {rep.examined or rep.modules} module(s) examined")
    else:
        print()
        print(rep.gate_detail())


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
    ap.add_argument("--format", choices=("text", "json", "github"),
                    default=None, dest="fmt",
                    help="report shape: human text (default), the full JSON "
                         "record, or GitHub Actions annotations")
    ap.add_argument("--json", action="store_true",
                    help="shorthand for --format json (the original spelling)")
    ap.add_argument("--facts", action="store_true",
                    help="print the page-router contract as prompt-ready "
                         "text and exit")
    args = ap.parse_args(argv)
    fmt = args.fmt or ("json" if args.json else "text")

    if not args.root.is_dir():
        print(f"import-gate: not a directory: {args.root}", file=sys.stderr)
        return 2

    if args.facts:
        print(_facts(args.root), end="")
        return 0

    rep = _gate(args.root, changed=args.changed)
    if fmt == "github":
        _print_github(rep, args.root)
    elif fmt == "json":
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
