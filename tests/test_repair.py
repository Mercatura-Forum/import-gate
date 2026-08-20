"""The additive repair: the two provable cases, and every refusal."""

from __future__ import annotations

from import_gate import check, repair
from import_gate import graph


def _recheck(root):
    graph._cache.clear()
    return check(root)


def test_default_import_of_a_named_export_gets_an_alias(tree):
    root = tree({
        "src/pages/Offers.tsx": "export function Offers() { return null }\n",
        "src/router.tsx": "import Offers from './pages/Offers'\n",
    })
    rep = repair(root, check(root))
    assert rep.files_touched == ["pages/Offers.tsx"]
    assert "export default Offers" in (root / "src/pages/Offers.tsx").read_text()
    assert _recheck(root).ok, "the repaired tree must pass its own check"


def test_named_import_of_a_named_default_gets_a_named_alias(tree):
    root = tree({
        "src/pages/About.tsx": "export default function About() { return null }\n",
        "src/router.tsx": "import { About } from './pages/About'\n",
    })
    rep = repair(root, check(root))
    assert rep.files_touched == ["pages/About.tsx"]
    assert "export { About }" in (root / "src/pages/About.tsx").read_text()
    assert _recheck(root).ok


def test_anonymous_default_is_left_for_the_author(tree):
    """`export default () => …` has no name to alias — never guessed."""
    root = tree({
        "src/pages/Menu.tsx": "export default () => null\n",
        "src/router.tsx": "import { Menu } from './pages/Menu'\n",
    })
    before = (root / "src/pages/Menu.tsx").read_text()
    rep = repair(root, check(root))
    assert rep.files_touched == []
    assert rep.left and "left for the author" in rep.left[0]
    assert (root / "src/pages/Menu.tsx").read_text() == before


def test_unresolved_module_is_never_created(tree):
    root = tree({
        "src/router.tsx": "import Gone from './pages/Gone'\n",
    })
    rep = repair(root, check(root))
    assert rep.files_touched == []
    assert not (root / "src/pages/Gone.tsx").exists()
    assert any("authoring decision" in l for l in rep.left)


def test_repair_never_edits_the_importer(tree):
    root = tree({
        "src/pages/Offers.tsx": "export function Offers() { return null }\n",
        "src/router.tsx": "import Offers from './pages/Offers'\n",
    })
    before = (root / "src/router.tsx").read_text()
    repair(root, check(root))
    assert (root / "src/router.tsx").read_text() == before
