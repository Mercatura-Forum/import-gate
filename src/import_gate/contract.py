"""The module contract: every relative import resolves, every binding exists.

A page and the file that imports it disagree about the export's shape more
often than either file is wrong on its own — the compiler then reports the
break AT THE IMPORTER (`TS2613 has no default export`, `TS2305 has no
exported member`), a file the author of the change may never have touched.
This module checks that contract structurally, before any compiler runs:

* `check()`   — every relative import in the tree (or in a named subset of
                files) must RESOLVE to a real module, and the binding it names
                must EXIST there: a default import needs a default export, a
                named import needs that named export. Violations carry the
                importer, the line, the target and the exact tsc code the
                compiler would raise.
* `repair()`  — ADDITIVE only. It appends the missing export form when — and
                only when — the binding provably already exists in the target
                under the other form. It never deletes, never rewrites, never
                touches an importer.
* `facts()`   — the contract stated as an obligation, ready to paste into a
                code-generation prompt: which pages the router imports, in
                which form, and therefore which export shape each page must
                keep.

The check costs one parse of a tree that is already on disk; the build it
pre-empts costs a full bundler + compiler run — and answers the same question
only as a wall of diagnostics after the expensive part.

What it refuses to judge: bare (package) specifiers — those are dependency
questions, owned by `gate` — and any module whose named-export set is OPEN
(`export * from './x'`), where absence cannot be proven.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import graph

#: cap on the facts block — a prompt is a budget, not a dump
MAX_FACT_CHARS = 1800


@dataclass
class Violation:
    """One broken edge of the import graph."""
    importer: str          # rel path from src/, e.g. "router.tsx"
    line: int
    target: str            # the specifier as written, e.g. "./pages/Offers"
    kind: str              # "unresolved" | "no-default-export" | "no-named-export"
    binding: str           # the imported name ("" for unresolved)
    ts_code: str           # the code tsc would raise for this
    detail: str
    repaired: bool = False

    def as_record(self) -> dict:
        return {
            "importer": self.importer, "line": self.line, "target": self.target,
            "kind": self.kind, "binding": self.binding, "ts_code": self.ts_code,
            "repaired": self.repaired, "detail": self.detail[:200],
        }


@dataclass
class ContractReport:
    method: str = "parser"           # "parser" | "regex-fallback" | "unavailable"
    modules: int = 0
    #: modules the extractor could not read. `check()` cannot judge a module it
    #: could not parse, so it skips one — which means every errored module is a
    #: silently UNCHECKED module. Counting them here is what makes that
    #: visible: "0 violations over 51 modules, 51 errored" is not the same
    #: statement as "0 violations over 51 modules", and without this field the
    #: two are indistinguishable.
    errored: int = 0
    violations: list[Violation] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def changed(self) -> bool:
        return bool(self.files_touched)

    def as_record(self) -> dict:
        return {
            "method": self.method,
            "modules": self.modules,
            "errored": self.errored,
            "n_violations": len(self.violations),
            "n_repaired": len(self.repaired),
            "n_left": len(self.left),
            "violations": [v.as_record() for v in self.violations[:20]],
            "files_touched": list(self.files_touched)[:20],
            "codes": sorted({v.ts_code for v in self.violations}),
        }


def check(root: Path, data: dict | None = None,
          only: list[str] | None = None) -> ContractReport:
    """Every relative import in the tree must resolve, and every binding it
    names must exist in the target.

    `only` restricts the IMPORTERS examined to those rel-paths (the edited
    files), but the whole graph is still extracted — editing a page can break
    the *router's* import, and the router is not in the edit set. So `only` is
    widened with every module that imports one of the named files.
    """
    root = Path(root)
    data = data if data is not None else graph.extract(root)
    modules = data.get("modules") or {}
    rep = ContractReport(method=data.get("method", "parser"),
                         modules=len(modules),
                         errored=graph.errored_modules(data))
    if not modules:
        rep.method = "unavailable"
        return rep

    importers = set(modules)
    if only:
        wanted = {Path(o).as_posix() for o in only}
        # direct edits …
        importers = {k for k in modules if k in wanted}
        # … plus everyone importing an edited module (the router case)
        for k, mod in modules.items():
            for imp in mod.get("imports") or []:
                spec = imp.get("spec") or ""
                if not graph.is_relative(spec):
                    continue
                tgt = graph.resolve(k, spec, modules)
                if tgt in wanted:
                    importers.add(k)
                    break

    for key in sorted(importers):
        mod = modules.get(key) or {}
        if mod.get("error"):
            continue
        for imp in mod.get("imports") or []:
            spec = imp.get("spec") or ""
            if not graph.is_relative(spec) or graph.is_asset(spec):
                continue
            target = graph.resolve(key, spec, modules)
            if target is None:
                rep.violations.append(Violation(
                    importer=key, line=int(imp.get("line") or 0), target=spec,
                    kind="unresolved", binding="", ts_code="TS2307",
                    detail=(f"{key}:{imp.get('line')} imports '{spec}', which "
                            "resolves to no module in this tree"),
                ))
                continue
            tmod = modules.get(target) or {}
            if tmod.get("error"):
                continue
            if imp.get("default"):
                if not tmod.get("hasDefaultExport"):
                    rep.violations.append(Violation(
                        importer=key, line=int(imp.get("line") or 0),
                        target=target, kind="no-default-export",
                        binding=str(imp["default"]), ts_code="TS2613",
                        detail=(f"{key}:{imp.get('line')} imports "
                                f"{imp['default']!r} as a DEFAULT import from "
                                f"'{spec}', but {target} has no default export "
                                f"(it exports "
                                f"{', '.join(tmod.get('namedExports') or []) or 'nothing'})"),
                    ))
            # a module with `export * from` has an OPEN named set — absence is
            # not provable there, so no named-export violation is raised
            if imp.get("named") and not tmod.get("starReexports"):
                have = set(tmod.get("namedExports") or [])
                for name in imp["named"]:
                    if name in have:
                        continue
                    rep.violations.append(Violation(
                        importer=key, line=int(imp.get("line") or 0),
                        target=target, kind="no-named-export", binding=name,
                        ts_code="TS2305",
                        detail=(f"{key}:{imp.get('line')} imports "
                                f"{{{name}}} from '{spec}', but {target} "
                                f"exports no such name (it exports "
                                f"{', '.join(sorted(have)) or 'nothing'}"
                                + (", and a default"
                                   if tmod.get("hasDefaultExport") else "")
                                + ")"),
                    ))
    return rep


# ── the repair — ADDITIVE ONLY ───────────────────────────────────────────────


def repair(root: Path, rep: ContractReport, data: dict | None = None,
           ) -> ContractReport:
    """Append the missing export form when the binding provably already exists
    in the target under the other form. Never deletes, never edits an importer.

    Two provable cases, and nothing else:

      * a DEFAULT import whose target exports the same name as a NAMED export
        → append `export default <Name>`;
      * a NAMED import `{X}` whose target's DEFAULT export is the *named*
        declaration `X` (`export default function X`) → append `export { X }`.

    An anonymous default (`export default () => …`), a name mismatch, or an
    unresolved module are all left for the author — with the reason named.
    """
    root = Path(root)
    data = data if data is not None else graph.extract(root)
    modules = data.get("modules") or {}
    src = root / "src"
    appended: dict[str, list[str]] = {}

    for v in rep.violations:
        tmod = modules.get(v.target) or {}
        if v.kind == "no-default-export":
            if v.binding in (tmod.get("namedExports") or []):
                appended.setdefault(v.target, []).append(
                    f"export default {v.binding}")
                v.repaired = True
                rep.repaired.append(
                    f"{v.target}: added `export default {v.binding}` — "
                    f"{v.importer}:{v.line} imports it as a default import")
            else:
                rep.left.append(
                    f"{v.target}: no default export and no named export "
                    f"{v.binding!r} to alias — left for the author")
        elif v.kind == "no-named-export":
            if tmod.get("defaultExportName") == v.binding:
                appended.setdefault(v.target, []).append(
                    f"export {{ {v.binding} }}")
                v.repaired = True
                rep.repaired.append(
                    f"{v.target}: added `export {{ {v.binding} }}` — "
                    f"{v.importer}:{v.line} imports it by name and the default "
                    f"export is the declaration {v.binding}")
            else:
                rep.left.append(
                    f"{v.target}: exports no {v.binding!r} and its default "
                    "export is not that declaration — left for the author")
        else:
            rep.left.append(
                f"{v.importer}:{v.line}: '{v.target}' resolves to no module — "
                "creating it is an authoring decision, not a mechanical one")

    for target, lines in sorted(appended.items()):
        path = src / target
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        add = "\n".join(sorted(set(lines)))
        sep = "" if text.endswith("\n") else "\n"
        path.write_text(f"{text}{sep}\n{add}\n", encoding="utf-8")
        rep.files_touched.append(target)
    return rep


# ── the contract as a FACT for code-generation prompts ───────────────────────


def facts(root: Path, data: dict | None = None,
          router: str = "router.tsx", cap: int = MAX_FACT_CHARS) -> str:
    """The page↔router contract, stated as an obligation a code generator
    must keep. Paste it into the prompt of whatever writes the pages.

    Empty string when there is no router module or nothing to say — never
    inject an empty section.
    """
    data = data if data is not None else graph.extract(Path(root))
    modules = data.get("modules") or {}
    router_mod = modules.get(router)
    if not router_mod:
        return ""
    default_pages: list[str] = []
    named_pages: list[str] = []
    for imp in router_mod.get("imports") or []:
        spec = imp.get("spec") or ""
        if not graph.is_relative(spec) or "/pages/" not in f"/{spec}":
            continue
        if imp.get("default"):
            default_pages.append(spec)
        elif imp.get("named"):
            named_pages.append(f"`{spec}` as {{{', '.join(imp['named'])}}}")
    if not (default_pages or named_pages):
        return ""
    default_pages.sort()
    named_pages.sort()

    # Stated as a RULE plus its exceptions rather than one line per page: the
    # per-page form spends most of a prompt budget saying the same thing N
    # times — and once it has to be truncated, a "COMPLETE" header over it
    # becomes false. This form is complete AND short.
    head = (
        f"### Page ↔ router module contract (machine-read from this tree — "
        "COMPLETE)\n"
        f"The `export` in a page and the `import` in {router} are ONE "
        "contract. Break it and the compiler reports TS2613 `has no default "
        "export` / TS2305 `has no exported member` AT THE ROUTER — a file you "
        "were not asked to edit.\n"
    )
    lines: list[str] = []
    if default_pages and not named_pages:
        lines.append(
            f"- All {len(default_pages)} pages {router} imports are DEFAULT "
            "imports (`import X from './pages/X'`), so every one of those pages "
            "must keep its `export default`.")
    elif default_pages:
        lines.append(
            f"- {len(default_pages)} pages are DEFAULT imports "
            "(`import X from './pages/X'`) and must keep `export default`.")
        lines.append(
            f"- EXCEPT these {len(named_pages)}, which {router} imports BY "
            "NAME and which must keep a matching named export (NOT a default): "
            + "; ".join(named_pages))
    else:
        lines.append(
            f"- All {len(named_pages)} pages {router} imports are NAMED "
            "imports and must keep their named export (NOT a default): "
            + "; ".join(named_pages))
    tail = (
        "- When you ADD a page, write the page's `export` and the router's "
        "`import` in the SAME form. When you EDIT a page, keep its export "
        "exactly as it is.\n")
    out = head + "\n".join(lines) + "\n" + tail
    if len(out) > cap:
        # drop the exception ROSTER before dropping the rule, and stop claiming
        # COMPLETE the moment anything is elided
        out = (head.replace(" — COMPLETE)", " — TRUNCATED, not complete)")
               + f"- {len(default_pages)} pages are DEFAULT imports and "
               f"{len(named_pages)} are NAMED imports; check {router} for the "
               "form of the page you touch before changing its export.\n" + tail)
    return out


def import_form_for(root: Path, page_stem: str,
                    data: dict | None = None) -> tuple[str, str]:
    """The import LINE a router must use for `src/pages/<page_stem>.tsx`,
    derived from that page's ACTUAL export shape, plus a one-word form name.

    For tooling that registers routes mechanically: an unconditional default
    import is wrong for any page written with a named export. Returns
    ("", "none") when the page cannot be read — the caller should then leave
    the registration to the author rather than guess.
    """
    data = data if data is not None else graph.extract(Path(root))
    modules = data.get("modules") or {}
    key = f"pages/{page_stem}.tsx"
    mod = modules.get(key) or modules.get(f"pages/{page_stem}.ts")
    if not mod:
        return "", "none"
    if mod.get("hasDefaultExport"):
        return (f"import {page_stem} from './pages/{page_stem}'", "default")
    if page_stem in (mod.get("namedExports") or []):
        return (f"import {{ {page_stem} }} from './pages/{page_stem}'", "named")
    return "", "none"
