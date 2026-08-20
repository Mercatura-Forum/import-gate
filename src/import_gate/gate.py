"""The gate: reject an unresolvable import BEFORE the expensive build runs.

## The problem this solves

A code generator (a model, a scaffolder, a codemod) writes
`import Card from '../components/ui/Card'` into a tree with no such file.
The full pipeline then runs — install, bundle, type-check, sometimes a
browser probe, minutes of it — and `tsc` reports TS2307 at the very end. A
whole repair cycle is spent on a diagnosis a string comparison could have
made before any of it started.

This module makes that comparison. It composes with `contract.check()` for
the relative half (one resolver, not two that can disagree) and adds the two
halves the contract deliberately refuses: bare package specifiers (judged
against `package.json`) and path aliases (judged against
`tsconfig.json`'s `compilerOptions.paths`).

## The one direction this gate is allowed to be wrong in

A false negative costs what you already pay today: one wasted build. A false
POSITIVE rejects a legitimate change, and in a retry loop it can spin a
change that was fine. Everything ambiguous therefore resolves to "accept",
and when the tree cannot be read at all the gate fails open LOUDLY — `ok` is
true and `method` records why, so a gate that stopped gating is visible in
your telemetry instead of looking like a clean tree. Classes a naive rule
would wrongly reject are each allowed explicitly: Node builtins (the bundler
resolves them without a package.json entry), asset imports, bundler query
suffixes (`?raw`), virtual modules, alias patterns that DO have a mapping,
scoped package roots (`@scope/name/subpath`), typings supplied by a
`@types/*` devDependency, and a package importing itself by its own name.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import contract, graph

#: Node builtins. A frontend rarely imports these, but the bundler resolves
#: them without a package.json entry, so rejecting one is a false positive.
_NODE_BUILTINS = frozenset({
    "assert", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "dns", "domain", "events", "fs", "http", "http2",
    "https", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder",
    "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib",
})

#: Alias-shaped prefixes. `@/` and `~/` are the conventional src aliases; `#`
#: is the package.json `imports` field. None resolve without a mapping.
_ALIAS_PREFIXES = ("@/", "~/")

#: The bundler resolves these itself; they are never package specifiers.
_VIRTUAL_PREFIXES = ("virtual:", "\0", "data:")

#: `//` comments and trailing commas appear in hand-written tsconfig files.
_JSON_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


@dataclass
class Rejection:
    """One import that cannot resolve, with everything a retry must name."""
    importer: str        # rel path from src/, e.g. "pages/About.tsx"
    line: int
    spec: str            # the specifier exactly as written
    kind: str            # "unresolved-relative" | "missing-package" | "unresolved-alias"
    ts_code: str         # the code tsc would raise — always TS2307 for these
    detail: str

    def as_record(self) -> dict:
        return {"importer": self.importer, "line": self.line, "spec": self.spec,
                "kind": self.kind, "ts_code": self.ts_code,
                "detail": self.detail[:200]}


@dataclass
class GateReport:
    method: str = "parser"          # "parser" | "regex-fallback" | "unavailable"
    modules: int = 0
    #: modules the extractor could not read. An errored module contributes no
    #: rejections — it has no parsed import list to check — so it is a module
    #: this gate did not gate. `rejected: 0` alone cannot distinguish a clean
    #: tree from an unreadable one; `errored` is what makes the zero
    #: falsifiable.
    errored: int = 0
    examined: int = 0
    rejections: list[Rejection] = field(default_factory=list)
    #: export-shape violations the contract found (TS2613/TS2305). Reported,
    #: never rejected on: this gate is scoped to imports that cannot RESOLVE,
    #: and `contract.repair()` can often fix shape mismatches additively.
    #: Naming them costs nothing and saves a cycle when the hint lands.
    advisories: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejections

    def as_record(self) -> dict:
        codes: dict[str, int] = {}
        for r in self.rejections:
            codes[r.ts_code] = codes.get(r.ts_code, 0) + 1
        return {
            "ok": self.ok,
            "method": self.method,
            "modules": self.modules,
            "errored": self.errored,
            "examined": self.examined,
            "rejected": len(self.rejections),
            "rejections": [r.as_record() for r in self.rejections],
            "advisories": self.advisories[:20],
            "ts_codes": codes,
        }

    def gate_detail(self) -> str:
        """The rejections rendered exactly as `tsc` would report them.

        `retry_text` is prose for a model or a human; this is the same facts
        in the compiler's own `file(line,col)` shape, for any tooling that
        parses diagnostics. Message-exact, position-approximate: the text
        is the diagnostic tsc would produce for the same tree, the path is
        src/-relative and the column is reported as 1 (tsc points at the
        specifier's own column).
        """
        return "\n".join(
            f"{r.importer}({r.line},1): error {r.ts_code}: "
            f"Cannot find module '{r.spec}' or its corresponding type declarations."
            for r in self.rejections
        )

    def retry_text(self) -> str:
        """The correction, stated so a code generator can act on it without
        guessing. It names every offending specifier with its file and line —
        a retry that only says "an import is wrong" costs the cycle the gate
        just saved.
        """
        if self.ok:
            return ""
        lines = [
            "Your change imports modules that do not exist. This was caught "
            "before the build ran, so nothing has been verified yet — fix the "
            "imports and the same change will proceed.",
            "",
        ]
        for r in self.rejections:
            if r.kind == "missing-package":
                why = (f"the package '{_pkg_root(graph.bare_spec(r.spec))}' is "
                       "not declared in package.json")
            elif r.kind == "unresolved-alias":
                why = ("this project defines no path alias — use a relative "
                       "specifier such as '../components/Thing'")
            else:
                why = "no such file in the tree (matching is case-sensitive)"
            lines.append(f"  {r.importer}:{r.line}  '{r.spec}' — {why}")
        lines += [
            "",
            "Import only modules that already exist in the tree, create the "
            "file you are importing in the same change, or declare the "
            "dependency in package.json first.",
        ]
        if self.advisories:
            lines += ["", "Also, these imports resolve but disagree with the "
                          "target's export shape:"]
            for a in self.advisories[:6]:
                lines.append(f"  {a.get('importer')}:{a.get('line')}  "
                             f"'{a.get('target')}' — {a.get('kind')}")
        return "\n".join(lines)


def _pkg_root(spec: str) -> str:
    """The package a bare specifier belongs to.

    `react-dom/client` -> `react-dom`; `@hookform/resolvers/zod` ->
    `@hookform/resolvers`. A scoped package's root is two segments — the
    single most likely thing for a naive rule to get wrong.
    """
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2])
    return parts[0]


def _types_package(root: str) -> str:
    """The `@types/*` package that would supply typings for `root`.

    `lodash` -> `@types/lodash`; `@scope/name` -> `@types/scope__name` (npm's
    own mangling). A type-only import satisfied by a devDependency `@types/x`
    is legitimate even when `x` itself is absent — and the acceptance applies
    to VALUE imports too, deliberately: telling the two apart wrongly would
    reject a legitimate change, and that is the direction this gate never
    errs in.
    """
    if root.startswith("@"):
        return "@types/" + root[1:].replace("/", "__")
    return "@types/" + root


def _declared(root: Path) -> tuple[set[str], str]:
    """Declared dependency names, and the package's own name for self-imports."""
    pj = root / "package.json"
    if not pj.is_file():
        return set(), ""
    try:
        j = json.loads(pj.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        # Unreadable package.json: we cannot prove a package is ABSENT, so the
        # package half must not reject at all. Returning an empty set would
        # reject every import instead, which is the one direction this gate
        # is not allowed to be wrong in — the caller checks for this.
        return set(), ""
    names = set(j.get("dependencies") or {}) | set(j.get("devDependencies") or {})
    names |= set(j.get("peerDependencies") or {})
    return names, str(j.get("name") or "")


def _alias_patterns(root: Path) -> list[str] | None:
    """`compilerOptions.paths` keys, if the project defines any — or None
    when alias absence CANNOT be proven, in which case the alias half must
    not reject at all (the same fail-open rule the package half applies to
    an unreadable package.json).

    Unprovable cases: a tsconfig that does not parse even after JSONC
    stripping, and a tsconfig that `extends` another file while defining no
    local `paths` — the mapping may live in the base config, and following
    the chain would mean re-implementing tsconfig resolution.

    A specifier matched by a known pattern is ACCEPTED without resolving the
    mapped target — verifying the target would mean a second resolver, and
    an untested resolver must not be allowed to reject a legitimate change.
    """
    tc = root / "tsconfig.json"
    if not tc.is_file():
        return []
    raw = _JSON_COMMENTS.sub("", tc.read_text(errors="replace"))
    raw = _TRAILING_COMMA.sub(r"\1", raw)
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return None                 # a mapping may exist in there — fail open
    paths = list(((j.get("compilerOptions") or {}).get("paths") or {}).keys())
    if not paths and j.get("extends"):
        return None                 # the mapping may live in the base config
    return paths


def _imports_patterns(root: Path) -> list[str] | None:
    """package.json `imports` keys (Node subpath imports, all `#`-prefixed) —
    or None when absence cannot be proven (unreadable package.json)."""
    pj = root / "package.json"
    if not pj.is_file():
        return []
    try:
        j = json.loads(pj.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return list((j.get("imports") or {}).keys())


def _matches_alias(spec: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p.endswith("*"):
            if spec.startswith(p[:-1]):
                return True
        elif spec == p:
            return True
    return False


def _norm_changed(changed: list[str] | None) -> list[str] | None:
    """Caller paths -> module keys (relative to `src/`).

    Callers report paths like `src/pages/About.tsx` or repo-relative
    variants; graph keys are `pages/About.tsx`. Non-module files are
    dropped: a changed `.css` has no imports to check.
    """
    if changed is None:
        return None
    out: list[str] = []
    for raw in changed:
        p = str(raw).replace("\\", "/").lstrip("./")
        i = p.find("src/")
        if i != -1:
            p = p[i + len("src/"):]
        if p.endswith((".ts", ".tsx")):
            out.append(p)
    return out


def _widen_for_deletions(only: list[str] | None, modules: dict) -> list[str] | None:
    """Add the importers of any changed file that no longer exists.

    `contract.check(only=…)` widens the examined set by RESOLVING each
    module's specifiers and keeping those that land on an edited file. That
    cannot see a deletion: a removed module resolves to None, so the file
    that still imports it is never added and the break goes unreported —
    which is the one case the widening exists for.

    So when a changed path is absent from the parsed graph (deleted or
    renamed), find its importers by the specifier's WOULD-BE target instead.
    Precise on purpose: falling back to a whole-tree check here would let a
    pre-existing break somewhere else reject an unrelated change.
    """
    if not only:
        return only
    missing = [k for k in only if k not in modules]
    if not missing:
        return only
    stems = set()
    for m in missing:
        stems.add(m)
        for ext in (".ts", ".tsx", ".d.ts"):
            if m.endswith(ext):
                stems.add(m[: -len(ext)])
    widened = list(only)
    for key, mod in modules.items():
        if key in widened:
            continue
        for imp in (mod.get("imports") or []):
            spec = imp.get("spec") if isinstance(imp, dict) else imp
            if not spec or not graph.is_relative(spec):
                continue
            cand = graph.join_rel(key, spec)
            if cand in stems or any(
                s == cand or s.startswith(cand + "/index.") for s in stems
            ):
                widened.append(key)
                break
    return widened


def gate(root: Path, changed: list[str] | None = None) -> GateReport:
    """Every import in the changed files must resolve. Deterministic, no
    model, no compiler.

    `root` is the package root (the directory holding `src/`, `package.json`
    and `tsconfig.json`). `changed` restricts the check to the files a change
    touched; `None` checks the whole tree.

    The two halves scope differently, on purpose:

    * The RELATIVE half is delegated to `contract.check(only=…)`, which
      widens the examined set with every module that imports a changed file —
      deleting or renaming a page breaks the *router*, and the router is
      never in the edit set.
    * The PACKAGE and ALIAS halves examine the changed files only. A package
      specifier's validity depends on `package.json` alone, so editing one
      file cannot break another file's package import, and widening would
      only let a pre-existing problem elsewhere reject an unrelated change.
    """
    root = Path(root)

    data = graph.extract(root)
    method = data.get("method", "parser")
    modules: dict = data.get("modules") or {}
    rep = GateReport(method=method, modules=len(modules),
                     errored=graph.errored_modules(data))

    if method == "unavailable" or not modules:
        # Nothing was read, so nothing can be proven absent. Fail open, and
        # let `method` say so — see the module docstring.
        return rep

    normed = _norm_changed(changed)
    if changed is not None and not normed:
        # A change set that normalizes to NOTHING (a CSS-only edit, a README)
        # has no imports to judge. Falling through would silently widen to a
        # whole-tree check, letting a pre-existing break elsewhere reject an
        # unrelated change — the exact false-positive direction this gate is
        # built to avoid.
        return rep
    only = _widen_for_deletions(normed, modules)

    # ── the relative half — delegated to the contract check ─────────────────
    ct = contract.check(root, data=data, only=only)
    for v in ct.violations:
        if v.kind == "unresolved":
            rep.rejections.append(Rejection(
                importer=v.importer, line=v.line, spec=v.target,
                kind="unresolved-relative", ts_code=v.ts_code or "TS2307",
                detail=v.detail,
            ))
        else:
            rep.advisories.append(v.as_record())

    # ── the package and alias halves ────────────────────────────────────────
    declared, own_name = _declared(root)
    alias_patterns = _alias_patterns(root)
    imports_patterns = _imports_patterns(root)
    # None means absence is unprovable — the corresponding half fails open.
    can_judge_aliases = alias_patterns is not None
    can_judge_imports = imports_patterns is not None
    # An unreadable or dependency-less package.json cannot prove absence.
    can_judge_packages = bool(declared)

    keys = list(modules) if only is None else [k for k in only if k in modules]
    rep.examined = len(keys) if only is not None else len(modules)

    for key in keys:
        for imp in (modules[key].get("imports") or []):
            spec = imp.get("spec") if isinstance(imp, dict) else imp
            if not spec or graph.is_relative(spec):
                continue  # the relative half owns these
            line = int(imp.get("line") or 0) if isinstance(imp, dict) else 0
            bare = graph.bare_spec(spec)

            if bare.startswith(_VIRTUAL_PREFIXES):
                continue                      # the bundler resolves these
            if bare.lower().endswith(graph.ASSET_SUFFIXES):
                continue                      # not a code module
            if can_judge_aliases and _matches_alias(bare, alias_patterns):
                continue                      # a mapping exists; see _alias_patterns
            if bare.startswith("node:") or _pkg_root(bare) in _NODE_BUILTINS:
                continue                      # bundler-resolved builtin

            if bare.startswith("#"):
                # Node subpath imports live in package.json's `imports` field,
                # not in tsconfig paths — judged (and failed open) separately
                if not can_judge_imports:
                    continue
                if _matches_alias(bare, imports_patterns):
                    continue
                rep.rejections.append(Rejection(
                    importer=key, line=line, spec=spec, kind="unresolved-alias",
                    ts_code="TS2307",
                    detail=(f"'{spec}' is a subpath import, and this project's "
                            "package.json `imports` field defines no mapping "
                            "for it"),
                ))
                continue

            if bare.startswith(_ALIAS_PREFIXES):
                if not can_judge_aliases:
                    continue                  # cannot prove absence — accept
                rep.rejections.append(Rejection(
                    importer=key, line=line, spec=spec, kind="unresolved-alias",
                    ts_code="TS2307",
                    detail=(f"'{spec}' is an alias, and this project defines no "
                            f"compilerOptions.paths mapping for it"),
                ))
                continue

            if not can_judge_packages:
                continue                      # cannot prove absence — accept
            pkg = _pkg_root(bare)
            if pkg == own_name or pkg in declared:
                continue
            if _types_package(pkg) in declared:
                continue                      # typings supplied by @types/*
            rep.rejections.append(Rejection(
                importer=key, line=line, spec=spec, kind="missing-package",
                ts_code="TS2307",
                detail=(f"package '{pkg}' is not declared in package.json "
                        "dependencies or devDependencies"),
            ))

    # Stable order so a retry message and a log record read the same twice.
    rep.rejections.sort(key=lambda r: (r.importer, r.line, r.spec))
    return rep
