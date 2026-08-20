"""The contract check: resolution order, case-sensitivity, export shapes."""

from __future__ import annotations

from import_gate import check, facts, import_form_for


def test_clean_tree_has_no_violations(tree):
    root = tree({
        "src/pages/Home.tsx": "export default function Home() { return null }\n",
        "src/router.tsx": "import Home from './pages/Home'\n",
    })
    rep = check(root)
    assert rep.ok and rep.modules == 2


def test_unresolved_relative_import_is_ts2307(tree):
    root = tree({
        "src/router.tsx": "import Gone from './pages/Gone'\n",
    })
    rep = check(root)
    assert [v.kind for v in rep.violations] == ["unresolved"]
    assert rep.violations[0].ts_code == "TS2307"
    assert rep.violations[0].importer == "router.tsx"


def test_resolution_is_case_sensitive(tree):
    """The macOS-to-CI classic: `./ui/card` against `ui/Card.tsx` resolves on
    a case-insensitive filesystem and breaks on Linux. The graph's keys never
    case-fold, so the gate catches it on every host."""
    root = tree({
        "src/ui/Card.tsx": "export default function Card() { return null }\n",
        "src/App.tsx": "import Card from './ui/card'\n",
    })
    rep = check(root)
    assert [v.kind for v in rep.violations] == ["unresolved"]


def test_extension_and_index_resolution(tree):
    root = tree({
        "src/lib/util.ts": "export const x = 1\n",
        "src/widgets/index.tsx": "export default function W() { return null }\n",
        "src/App.tsx": ("import { x } from './lib/util'\n"
                        "import W from './widgets'\n"),
    })
    assert check(root).ok


def test_default_import_needs_a_default_export(tree):
    root = tree({
        "src/pages/Offers.tsx": "export function Offers() { return null }\n",
        "src/router.tsx": "import Offers from './pages/Offers'\n",
    })
    rep = check(root)
    assert [v.kind for v in rep.violations] == ["no-default-export"]
    assert rep.violations[0].ts_code == "TS2613"


def test_named_import_needs_that_named_export(tree):
    root = tree({
        "src/pages/About.tsx": "export default function About() { return null }\n",
        "src/router.tsx": "import { About } from './pages/About'\n",
    })
    rep = check(root)
    assert [v.kind for v in rep.violations] == ["no-named-export"]
    assert rep.violations[0].ts_code == "TS2305"


def test_star_reexport_keeps_the_named_set_open(tree):
    """`export * from` makes absence unprovable — no named violation there."""
    root = tree({
        "src/lib/impl.ts": "export const deep = 1\n",
        "src/lib/barrel.ts": "export * from './impl'\n",
        "src/App.tsx": "import { anything } from './lib/barrel'\n",
    })
    assert check(root).ok


def test_asset_and_query_suffix_imports_are_not_violations(tree):
    root = tree({
        "src/style.css": "/* not a module */",
        "src/App.tsx": ("import './style.css'\n"
                        "import raw from './style.css?raw'\n"),
    })
    assert check(root).ok


def test_only_scoping_still_sees_the_importer(tree):
    """Editing a page can break the ROUTER's import; the router is not in the
    edit set, so `only` is widened with everyone importing an edited file."""
    root = tree({
        "src/pages/Home.tsx": "export function Home() { return null }\n",
        "src/router.tsx": "import Home from './pages/Home'\n",
    })
    rep = check(root, only=["pages/Home.tsx"])
    assert [v.importer for v in rep.violations] == ["router.tsx"]
    assert rep.violations[0].kind == "no-default-export"


def test_facts_states_the_rule_and_the_exceptions(tree):
    root = tree({
        "src/pages/Home.tsx": "export default function Home() { return null }\n",
        "src/pages/Team.tsx": "export function Team() { return null }\n",
        "src/router.tsx": ("import Home from './pages/Home'\n"
                           "import { Team } from './pages/Team'\n"),
    })
    text = facts(root)
    assert "COMPLETE" in text
    assert "1 pages are DEFAULT imports" in text
    assert "Team" in text and "NOT a default" in text


def test_facts_is_empty_without_a_router(tree):
    root = tree({"src/lib/a.ts": "export const a = 1\n"})
    assert facts(root) == ""


def test_import_form_follows_the_pages_actual_shape(tree):
    root = tree({
        "src/pages/Home.tsx": "export default function Home() { return null }\n",
        "src/pages/Team.tsx": "export function Team() { return null }\n",
    })
    line, form = import_form_for(root, "Home")
    assert form == "default" and line == "import Home from './pages/Home'"
    line, form = import_form_for(root, "Team")
    assert form == "named" and line == "import { Team } from './pages/Team'"
    assert import_form_for(root, "Missing") == ("", "none")
