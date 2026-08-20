"""Edge behaviors surfaced by the pre-publish review — each was either an
actual bug (fixed, pinned here) or an untested branch a plausible user input
reaches."""

from __future__ import annotations

import json

from import_gate import check, facts, gate, repair
from import_gate import graph
from import_gate.cli import main as cli_main

PAGE = "export default function App() { return null }\n"


# ── the two review blockers, fixed ───────────────────────────────────────────


def test_css_only_change_never_widens_to_the_whole_tree(tree):
    """A change set that normalizes to nothing has no imports to judge —
    falling through to a whole-tree check let a pre-existing break reject an
    unrelated stylesheet edit."""
    root = tree({
        "src/legacy/Old.tsx": "import Gone from './missing/Gone'\n",
        "src/style.css": "body {}\n",
    }, deps={"react": "^18.0.0"})
    rep = gate(root, changed=["src/style.css"])
    assert rep.ok and rep.examined == 0
    assert not gate(root).ok, "…while the whole-tree check still sees the break"


def test_subpath_import_with_a_mapping_is_accepted(tree):
    root = tree({"src/App.tsx": "import { env } from '#config'\n" + PAGE})
    (root / "package.json").write_text(json.dumps(
        {"dependencies": {"react": "^18.0.0"},
         "imports": {"#config": "./src/config.ts"}}))
    graph._cache.clear()
    assert gate(root).ok


def test_subpath_import_without_a_mapping_is_rejected(tree):
    root = tree({"src/App.tsx": "import { env } from '#config'\n" + PAGE},
                deps={"react": "^18.0.0"})
    rep = gate(root)
    assert [r.kind for r in rep.rejections] == ["unresolved-alias"]
    assert "imports" in rep.rejections[0].detail


# ── fail-open: every unprovable-absence branch ───────────────────────────────


def test_unparseable_tsconfig_fails_open_for_aliases(tree):
    root = tree({"src/App.tsx": "import B from '@/components/B'\n" + PAGE},
                deps={"react": "^18.0.0"},
                tsconfig='{"compilerOptions": {"paths": {"@/*": OOPS}}}')
    assert gate(root).ok, "a mapping may exist in the unparseable file"


def test_tsconfig_extends_without_local_paths_fails_open(tree):
    root = tree({"src/App.tsx": "import B from '@/components/B'\n" + PAGE},
                deps={"react": "^18.0.0"},
                tsconfig={"extends": "./tsconfig.base.json"})
    assert gate(root).ok, "the mapping may live in the base config"


def test_malformed_package_json_fails_open_for_packages(tree):
    root = tree({"src/App.tsx": "import { x } from 'mystery-lib'\n" + PAGE})
    (root / "package.json").write_text('{"dependencies": {"react": OOPS}}')
    graph._cache.clear()
    assert gate(root).ok


# ── resolution completeness ─────────────────────────────────────────────────


def test_directory_resolves_to_index_dts_and_index_ts(tree):
    root = tree({
        "src/lib/index.d.ts": "export declare const x: number\n",
        "src/util/index.ts": "export const y = 1\n",
        "src/App.tsx": ("import { x } from './lib'\n"
                        "import { y } from './util'\n" + PAGE),
    })
    assert check(root).ok


def test_direct_dts_resolution(tree):
    root = tree({
        "src/types/api.d.ts": "export declare const api: string\n",
        "src/App.tsx": "import { api } from './types/api'\n" + PAGE,
    })
    assert check(root).ok


def test_query_suffix_on_a_code_module_skips_shape_checks(tree):
    """`./shader?raw` imports the transform's OUTPUT (a string), not the
    module's bindings — a default import of it must not read as TS2613."""
    root = tree({
        "src/shader.ts": "export const s = 'void main() {}'\n",
        "src/App.tsx": "import src from './shader?raw'\n" + PAGE,
    }, deps={"react": "^18.0.0"})
    rep = gate(root)
    assert rep.ok and not rep.advisories


def test_query_suffix_on_a_missing_file_still_rejects(tree):
    root = tree({
        "src/App.tsx": "import src from './missing?raw'\n" + PAGE,
    }, deps={"react": "^18.0.0"})
    assert [r.kind for r in gate(root).rejections] == ["unresolved-relative"]


# ── import forms ─────────────────────────────────────────────────────────────


def test_namespace_import_checks_resolution_but_never_shape(tree):
    root = tree({
        "src/lib/util.ts": "export const a = 1\n",
        "src/App.tsx": ("import * as util from './lib/util'\n"
                        "import * as gone from './lib/gone'\n" + PAGE),
    })
    rep = check(root)
    assert [v.kind for v in rep.violations] == ["unresolved"]


def test_side_effect_imports(tree):
    root = tree({
        "src/setup.ts": "console.log('side effect')\n",
        "src/App.tsx": ("import './setup'\n"
                        "import './polyfills'\n"
                        "import 'core-js'\n" + PAGE),
    }, deps={"react": "^18.0.0"})
    rep = gate(root)
    kinds = sorted(r.kind for r in rep.rejections)
    assert kinds == ["missing-package", "unresolved-relative"]


def test_relative_type_only_import_still_must_resolve(tree):
    root = tree({
        "src/App.tsx": "import type { T } from './types/gone'\n" + PAGE,
    })
    assert [v.kind for v in check(root).violations] == ["unresolved"]


# ── declared-dependency shapes ───────────────────────────────────────────────


def test_peer_and_dev_dependencies_are_accepted(tree):
    root = tree({"src/App.tsx": ("import { a } from 'peer-lib'\n"
                                 "import { b } from 'dev-lib'\n" + PAGE)})
    (root / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18.0.0"},
        "devDependencies": {"dev-lib": "^1.0.0"},
        "peerDependencies": {"peer-lib": "^1.0.0"},
    }))
    graph._cache.clear()
    assert gate(root).ok


def test_exact_alias_pattern_matches_without_a_wildcard(tree):
    root = tree({"src/App.tsx": "import cfg from '~/config'\n" + PAGE},
                deps={"react": "^18.0.0"},
                tsconfig={"compilerOptions": {"paths": {"~/config": ["./src/config.ts"]}}})
    assert gate(root).ok


# ── changed-path normalization ───────────────────────────────────────────────


def test_changed_paths_with_backslashes_and_dot_prefix(tree):
    root = tree({
        "src/pages/Home.tsx": "import Gone from './Gone'\n" + PAGE,
    }, deps={"react": "^18.0.0"})
    for form in (r"src\pages\Home.tsx", "./src/pages/Home.tsx"):
        rep = gate(root, changed=[form])
        assert [r.kind for r in rep.rejections] == ["unresolved-relative"], form


# ── honest degradation ───────────────────────────────────────────────────────


def test_mostly_errored_parse_falls_back_and_says_so(tree, monkeypatch):
    root = tree({
        "src/a.ts": "export const a = 1\n",
        "src/b.ts": "export const b = 2\n",
    })
    poisoned = {"method": "parser", "modules": {
        "a.ts": {"error": "EACCES"}, "b.ts": {"error": "EACCES"},
    }}
    monkeypatch.setattr(graph, "_extract_node", lambda _root: poisoned)
    graph._cache.clear()
    data = graph.extract(root)
    assert data["method"] == "regex-fallback"
    assert data["fallback_reason"] == "parser-error-rate"
    assert data["parser_errored"] == 2 and data["parser_modules"] == 2


def test_errored_modules_are_counted_and_skipped(tree, monkeypatch):
    partial = {"method": "parser", "modules": {
        "ok.ts": {"hasDefaultExport": False, "defaultExportName": None,
                  "namedExports": ["fine"], "starReexports": [],
                  "imports": [{"spec": "./broken", "line": 1, "default": None,
                               "named": ["x"], "namespace": None,
                               "typeOnly": False, "sideEffectOnly": False}]},
        "broken.ts": {"error": "EACCES"},
    }}
    root = tree({"src/ok.ts": "placeholder\n", "src/broken.ts": "x\n"})
    monkeypatch.setattr(graph, "_extract_node", lambda _root: partial)
    graph._cache.clear()
    rep = check(root)
    assert rep.errored == 1
    assert rep.ok, "an errored target must not be judged — skipped, counted"


def test_repair_refuses_a_target_missing_from_disk(tree, monkeypatch):
    """The graph may be stale: a target parsed a moment ago can be gone by
    repair time. The report must not claim a repair it never wrote."""
    root = tree({
        "src/router.tsx": "import Offers from './pages/Offers'\n",
        "src/pages/Offers.tsx": "export function Offers() { return null }\n",
    })
    rep = check(root)
    (root / "src/pages/Offers.tsx").unlink()
    rep = repair(root, rep)
    assert rep.files_touched == [] and rep.repaired == []
    assert any("not a file on disk" in l for l in rep.left)
    assert not any(v.repaired for v in rep.violations)


# ── facts() branches ─────────────────────────────────────────────────────────


def test_facts_truncation_stops_claiming_complete(tree):
    files = {"src/router.tsx": "".join(
        f"import {{ P{i} }} from './pages/P{i}'\n" for i in range(30))}
    for i in range(30):
        files[f"src/pages/P{i}.tsx"] = f"export function P{i}() {{ return null }}\n"
    root = tree(files)
    text = facts(root, cap=400)
    assert "TRUNCATED, not complete" in text
    assert "COMPLETE" not in text.replace("TRUNCATED, not complete", "")


def test_facts_all_named_phrasing(tree):
    root = tree({
        "src/pages/A.tsx": "export function A() { return null }\n",
        "src/router.tsx": "import { A } from './pages/A'\n",
    })
    text = facts(root)
    assert "All 1 pages" in text and "NAMED" in text


# ── the CLI, end to end ──────────────────────────────────────────────────────


def test_cli_exit_codes_and_json(tree, capsys):
    root = tree({"src/App.tsx": "import Gone from './Gone'\n" + PAGE},
                deps={"react": "^18.0.0"})
    assert cli_main([str(root)]) == 1
    out = capsys.readouterr().out
    assert "TS2307" in out and "case-sensitive" in out

    assert cli_main([str(root), "--json"]) == 1
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is False and rec["rejected"] == 1

    assert cli_main([str(root / "does-not-exist")]) == 2


def test_cli_ok_line_and_facts(tree, capsys):
    root = tree({
        "src/pages/Home.tsx": "export default function Home() { return null }\n",
        "src/router.tsx": "import Home from './pages/Home'\n",
    }, deps={"react": "^18.0.0"})
    assert cli_main([str(root)]) == 0
    assert "import-gate: ok" in capsys.readouterr().out
    assert cli_main([str(root), "--facts"]) == 0
    assert "module contract" in capsys.readouterr().out
