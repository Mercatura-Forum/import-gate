"""The import/export graph of a TypeScript tree, and how specifiers resolve.

Extraction is parser-first: a small Node script parses every module with the
TypeScript compiler API (a pure syntactic parse — no type-check, no
node_modules needed for the tree under inspection). When Node or a usable
`typescript` is unavailable, a conservative regex extractor takes over and
MARKS itself, so a weaker reading is never mistaken for the parser's.

Resolution is deliberately filesystem-free: a specifier resolves against the
graph's keys, which come from a case-sensitive directory walk. That makes the
check catch the classic macOS-to-Linux failure — `import './ui/card'` against
`ui/Card.tsx` resolves on a case-insensitive filesystem and breaks in CI —
because dictionary keys never case-fold.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_EXTRACT_MJS = Path(__file__).with_name("extract.mjs")

#: extensions tried when resolving a relative specifier, in TypeScript's order
RESOLVE_EXTS = (".ts", ".tsx", ".d.ts")
#: …then as a directory with an index file (index.d.ts included: TypeScript
#: resolves a directory to its type-declaration index too, and omitting it
#: makes the gate reject an import tsc accepts)
RESOLVE_INDEX = ("index.ts", "index.tsx", "index.d.ts")
#: non-code specifiers a frontend legitimately imports; not import questions
ASSET_SUFFIXES = (".css", ".scss", ".svg", ".png", ".jpg", ".jpeg", ".webp",
                  ".gif", ".json", ".ico", ".woff", ".woff2")


# ── extraction ───────────────────────────────────────────────────────────────


def _extract_node(root: Path) -> dict | None:
    try:
        proc = subprocess.run(
            ["node", str(_EXTRACT_MJS), str(root)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    return data


_RE_IMPORT = re.compile(
    r"^import\s+(?P<clause>type\s+)?(?P<body>[^'\"]*?)\s*from\s*['\"](?P<spec>[^'\"]+)['\"]",
    re.M)
_RE_SIDE_EFFECT_IMPORT = re.compile(r"^import\s*['\"](?P<spec>[^'\"]+)['\"]", re.M)
_RE_EXPORT_DEFAULT_DECL = re.compile(
    r"^export\s+default\s+(?:async\s+)?(?:function|class)\s+(?P<name>\w+)", re.M)
_RE_EXPORT_DEFAULT_ANY = re.compile(r"^export\s+default\b", re.M)
_RE_EXPORT_DEFAULT_IDENT = re.compile(r"^export\s+default\s+(?P<name>\w+)\s*$", re.M)
_RE_EXPORT_NAMED_DECL = re.compile(
    # `declare` rides along in .d.ts files: `export declare const x`
    r"^export\s+(?:declare\s+)?(?:async\s+)?"
    r"(?:function|class|const|let|var|interface|type|enum)\s+(?P<name>\w+)",
    re.M)
_RE_EXPORT_BRACE = re.compile(r"^export\s*\{(?P<body>[^}]*)\}", re.M)
_RE_EXPORT_STAR = re.compile(r"^export\s*\*\s*from\s*['\"](?P<spec>[^'\"]+)['\"]", re.M)


def _extract_regex(root: Path) -> dict:
    """Fallback when the real parser cannot run (no Node, no `typescript`).

    Same output shape and same keys; marks itself `regex-fallback` so a weaker
    extraction is never mistaken for the parser's. It is deliberately
    CONSERVATIVE: a construct it cannot read confidently contributes no
    violation downstream.
    """
    src = root / "src"
    modules: dict = {}
    if not src.is_dir():
        return {"method": "regex-fallback", "modules": {}}
    for p in sorted(src.rglob("*")):
        if p.suffix not in (".ts", ".tsx") or not p.is_file():
            continue
        if "node_modules" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        imports = []
        for m in _RE_IMPORT.finditer(text):
            body = (m.group("body") or "").strip()
            line = text[: m.start()].count("\n") + 1
            rec = {"spec": m.group("spec"), "line": line, "default": None,
                   "named": [], "namespace": None,
                   "typeOnly": bool(m.group("clause")), "sideEffectOnly": False}
            brace = re.search(r"\{(?P<n>[^}]*)\}", body)
            head = body[: brace.start()] if brace else body
            head = head.rstrip(",").strip()
            ns = re.search(r"\*\s*as\s+(\w+)", head)
            if ns:
                rec["namespace"] = ns.group(1)
            elif head and re.fullmatch(r"\w+", head):
                rec["default"] = head
            if brace:
                for part in brace.group("n").split(","):
                    part = part.strip()
                    if not part:
                        continue
                    part = re.sub(r"^type\s+", "", part)
                    exported = part.split(" as ")[0].strip()
                    if re.fullmatch(r"\w+", exported):
                        rec["named"].append(exported)
                rec["named"].sort()
            imports.append(rec)
        for m in _RE_SIDE_EFFECT_IMPORT.finditer(text):
            imports.append({"spec": m.group("spec"),
                            "line": text[: m.start()].count("\n") + 1,
                            "default": None, "named": [], "namespace": None,
                            "typeOnly": False, "sideEffectOnly": True})
        named = {m.group("name") for m in _RE_EXPORT_NAMED_DECL.finditer(text)}
        for m in _RE_EXPORT_BRACE.finditer(text):
            for part in m.group("body").split(","):
                part = part.strip()
                if not part:
                    continue
                part = re.sub(r"^type\s+", "", part)
                local, _, alias = part.partition(" as ")
                name = (alias or local).strip()
                if re.fullmatch(r"\w+", name):
                    named.add(name)
        dflt = _RE_EXPORT_DEFAULT_DECL.search(text)
        dflt_ident = _RE_EXPORT_DEFAULT_IDENT.search(text)
        modules[p.relative_to(src).as_posix()] = {
            "hasDefaultExport": bool(_RE_EXPORT_DEFAULT_ANY.search(text))
            or "default" in named,
            "defaultExportName": (dflt.group("name") if dflt
                                  else dflt_ident.group("name") if dflt_ident
                                  else None),
            "namedExports": sorted(named - {"default"}),
            "starReexports": sorted(m.group("spec")
                                    for m in _RE_EXPORT_STAR.finditer(text)),
            "imports": imports,
        }
    return {"method": "regex-fallback", "modules": modules}


#: Fraction of unreadable modules above which the parse is treated as a
#: toolchain failure rather than a property of the tree, and the regex fallback
#: takes over. Half is the honest line: TypeScript's parser is error-TOLERANT —
#: a syntax error yields a SourceFile with diagnostics, not a throw — so a
#: per-file error means the file read or the API itself failed, and a majority
#: of those means it will fail on the next file too. Below the line a real
#: per-file error (an unreadable file, a permissions problem) is still counted
#: through `errored` without discarding a parse that otherwise worked.
PARSER_ERROR_FALLBACK_RATIO = 0.5


def _parser_is_degraded(data: dict) -> bool:
    mods = (data or {}).get("modules") or {}
    if not mods:
        return False
    return errored_modules(data) / len(mods) > PARSER_ERROR_FALLBACK_RATIO


#: content-hash cache: the graph is a pure function of the tree, and a caller
#: typically asks for it several times per change (facts for a prompt, the
#: contract check, the gate). Bounded so a long-lived process cannot grow it
#: without limit.
_cache: dict[str, dict] = {}
_CACHE_MAX = 8


def _content_key(root: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    for p in sorted((root / "src").rglob("*")):
        if p.suffix not in (".ts", ".tsx") or not p.is_file():
            continue
        if "node_modules" in p.parts:
            continue
        h.update(p.relative_to(root).as_posix().encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()


def errored_modules(data: dict) -> int:
    """How many modules the extractor could not read."""
    mods = (data or {}).get("modules") or {}
    return sum(1 for v in mods.values() if isinstance(v, dict) and v.get("error"))


def extract(root: Path) -> dict:
    """The import/export graph, parser-first with a marked regex fallback.

    `root` is the package root — the directory holding `src/` (and usually
    `package.json` and `tsconfig.json`). Short-circuits on a tree with no
    `src/`: spawning Node to parse nothing is pure latency.
    """
    root = Path(root)
    if not (root / "src").is_dir():
        return {"method": "unavailable", "modules": {}}
    key = _content_key(root)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    data = _extract_node(root)
    if data is None:
        data = _extract_regex(root)
    elif _parser_is_degraded(data):
        # The parser ran and produced a well-formed payload, but could not read
        # most of the tree. That is a statement about the TOOLCHAIN, not about
        # the code (see PARSER_ERROR_FALLBACK_RATIO). Left as-is this would be
        # a silent-acceptance path: `check()` skips every errored module, so an
        # unreadable tree reports zero violations and a gate that depends on it
        # stops gating while still reporting `method: "parser"`. Falling back
        # keeps the gate GATING on a degraded-but-real reading, and the
        # counters keep the degradation visible rather than implicit.
        n_err, n_tot = errored_modules(data), len(data.get("modules") or {})
        data = _extract_regex(root)
        data["fallback_reason"] = "parser-error-rate"
        data["parser_errored"] = n_err
        data["parser_modules"] = n_tot
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = data
    return data


# ── resolution ───────────────────────────────────────────────────────────────


def bare_spec(spec: str) -> str:
    """Strip a bundler query suffix: `./a.css?raw` -> `./a.css`.

    `?raw`, `?url` and `?inline` instruct the bundler (Vite et al.); they are
    not part of the module path. Without this, a stylesheet import with a
    query suffix reads as an unresolved code module.
    """
    return spec.split("?", 1)[0]


def join_rel(importer_rel: str, spec: str) -> str:
    """Normalise a relative specifier against its importer, without touching
    the filesystem.

    Split out of `resolve` because a caller sometimes needs the WOULD-BE
    target of a specifier whose file is gone: `resolve` returns None for a
    deleted module, so discovering who imported the file an edit removed
    requires computing the target path directly.
    """
    base = Path(importer_rel).parent
    target = (base / bare_spec(spec)).as_posix()
    # normalise "./" and "../" segments without touching the filesystem
    parts: list[str] = []
    for seg in target.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def resolve(importer_rel: str, spec: str, modules: dict) -> str | None:
    """Resolve a RELATIVE specifier to a key of `modules`, or None.

    `importer_rel` and the returned key are both relative to `src/`. Bare
    (package) specifiers return None and are never treated as violations —
    `is_relative()` is the caller's guard. Matching is case-sensitive by
    construction: the keys come from the directory walk, and dict lookup
    never case-folds.
    """
    target = join_rel(importer_rel, spec)
    if target in modules:
        return target
    for ext in RESOLVE_EXTS:
        if target + ext in modules:
            return target + ext
    # an explicit .ts/.tsx extension was already handled above
    for idx in RESOLVE_INDEX:
        cand = f"{target}/{idx}"
        if cand in modules:
            return cand
    return None


def is_relative(spec: str) -> bool:
    return spec.startswith("./") or spec.startswith("../")


def is_asset(spec: str) -> bool:
    return bare_spec(spec).lower().endswith(ASSET_SUFFIXES)
