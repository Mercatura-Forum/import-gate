"""The gate: package/alias halves, fail-open posture, deletion widening."""

from __future__ import annotations

from import_gate import gate


PAGE = "export default function App() { return null }\n"


def test_missing_package_is_rejected_with_the_root_named(tree):
    root = tree({
        "src/App.tsx": "import { useForm } from 'react-hook-form'\n" + PAGE,
    }, deps={"react": "^18.0.0"})
    rep = gate(root)
    assert [r.kind for r in rep.rejections] == ["missing-package"]
    assert "react-hook-form" in rep.rejections[0].detail
    assert rep.rejections[0].ts_code == "TS2307"


def test_scoped_roots_and_subpaths_resolve_by_the_root_rule(tree):
    root = tree({
        "src/App.tsx": ("import { z } from '@hookform/resolvers/zod'\n"
                        "import { createRoot } from 'react-dom/client'\n"
                        + PAGE),
    }, deps={"@hookform/resolvers": "^3.0.0", "react-dom": "^18.0.0"})
    assert gate(root).ok


def test_types_package_satisfies_a_bare_import(tree):
    root = tree({
        "src/App.tsx": "import type { Thing } from 'somelib'\n" + PAGE,
    }, deps={"@types/somelib": "^1.0.0"})
    assert gate(root).ok


def test_node_builtins_and_virtual_modules_are_accepted(tree):
    root = tree({
        "src/App.tsx": ("import { join } from 'path'\n"
                        "import { x } from 'node:crypto'\n"
                        "import v from 'virtual:generated'\n" + PAGE),
    }, deps={"react": "^18.0.0"})
    assert gate(root).ok


def test_self_import_by_package_name_is_accepted(tree):
    root = tree({
        "src/App.tsx": "import { thing } from 'myapp'\n" + PAGE,
    }, deps={"react": "^18.0.0"}, name="myapp")
    assert gate(root).ok


def test_alias_without_a_mapping_is_rejected(tree):
    root = tree({
        "src/App.tsx": "import Button from '@/components/Button'\n" + PAGE,
    }, deps={"react": "^18.0.0"})
    rep = gate(root)
    assert [r.kind for r in rep.rejections] == ["unresolved-alias"]


def test_alias_with_a_paths_mapping_is_accepted(tree):
    root = tree({
        "src/App.tsx": "import Button from '@/components/Button'\n" + PAGE,
    }, deps={"react": "^18.0.0"},
        tsconfig={"compilerOptions": {"paths": {"@/*": ["./src/*"]}}})
    assert gate(root).ok


def test_jsonc_tsconfig_is_read(tree):
    """Hand-written tsconfig files carry // comments and trailing commas."""
    root = tree({
        "src/App.tsx": "import Button from '@/components/Button'\n" + PAGE,
    }, deps={"react": "^18.0.0"},
        tsconfig=('{\n  // path aliases\n  "compilerOptions": {\n'
                  '    "paths": { "@/*": ["./src/*"], },\n  },\n}\n'))
    assert gate(root).ok


def test_no_dependencies_declared_fails_open_on_packages(tree):
    """An empty or absent dependency map cannot prove a package ABSENT."""
    root = tree({
        "src/App.tsx": "import { x } from 'mystery-lib'\n" + PAGE,
    })
    rep = gate(root)
    assert rep.ok, "without a package.json the package half must not reject"


def test_unreadable_tree_fails_open_loudly(tmp_path):
    rep = gate(tmp_path)  # no src/ at all
    assert rep.ok
    assert rep.method == "unavailable"


def test_changed_scoping_ignores_unrelated_preexisting_breaks(tree):
    root = tree({
        "src/legacy/Old.tsx": "import { gone } from 'not-installed'\n",
        "src/pages/New.tsx": PAGE,
    }, deps={"react": "^18.0.0"})
    rep = gate(root, changed=["src/pages/New.tsx"])
    assert rep.ok, "a pre-existing break elsewhere must not reject this change"
    assert not gate(root).ok, "…but the whole-tree check still sees it"


def test_deleting_a_file_surfaces_the_orphaned_importer(tree):
    """The widening's whole reason: a deleted page resolves to None, so its
    importer is found by the specifier's WOULD-BE target instead."""
    root = tree({
        "src/router.tsx": "import Home from './pages/Home'\n",
    }, deps={"react": "^18.0.0"})
    # the change set says pages/Home.tsx changed — but it is GONE
    rep = gate(root, changed=["src/pages/Home.tsx"])
    assert [r.kind for r in rep.rejections] == ["unresolved-relative"]
    assert rep.rejections[0].importer == "router.tsx"


def test_export_shape_mismatches_are_advisories_not_rejections(tree):
    root = tree({
        "src/pages/Offers.tsx": "export function Offers() { return null }\n",
        "src/router.tsx": "import Offers from './pages/Offers'\n",
    }, deps={"react": "^18.0.0"})
    rep = gate(root)
    assert rep.ok
    assert rep.advisories and rep.advisories[0]["kind"] == "no-default-export"


def test_gate_detail_renders_the_compilers_own_shape(tree):
    root = tree({
        "src/App.tsx": "import Gone from './missing/Gone'\n",
    }, deps={"react": "^18.0.0"})
    rep = gate(root)
    assert not rep.ok
    line = rep.gate_detail().splitlines()[0]
    assert line.startswith("App.tsx(1,1): error TS2307: Cannot find module")


def test_retry_text_names_every_offender(tree):
    root = tree({
        "src/App.tsx": ("import A from './gone/A'\n"
                        "import { b } from 'not-a-dep'\n" + PAGE),
    }, deps={"react": "^18.0.0"})
    text = gate(root).retry_text()
    assert "./gone/A" in text and "not-a-dep" in text
    assert "case-sensitive" in text
    assert "App.tsx:1" in text and "App.tsx:2" in text
