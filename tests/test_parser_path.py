"""The real Node/typescript extractor — skipped when the toolchain is absent.

These tests assert only what is EXTRA about the parser over the fallback
(aliased named imports resolve to the exported name; the method marker), so a
host without Node still proves the full gate logic through the other modules.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from import_gate import graph

_MJS = Path(graph.__file__).with_name("extract.mjs")


def _parser_available() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        ["node", "-e",
         "const ts=require('typescript');"
         "process.exit(typeof ts.createSourceFile==='function'?0:1)"],
        capture_output=True, cwd=str(_MJS.parent))
    return probe.returncode == 0


pytestmark = [
    pytest.mark.parser_path,
    pytest.mark.skipif(not _parser_available(),
                       reason="node + a usable typescript are not available"),
]


def test_parser_marks_itself_and_reads_aliased_imports(tree):
    root = tree({
        "src/lib/util.ts": "export const realName = 1\n",
        "src/App.tsx": "import { realName as alias } from './lib/util'\n"
                       "export default function App() { return null }\n",
    })
    graph._cache.clear()
    data = graph.extract(root)
    assert data["method"] == "parser"
    imp = data["modules"]["App.tsx"]["imports"][0]
    # `import { A as B }` — the contract is on A, the exported name
    assert imp["named"] == ["realName"]


def test_parser_and_fallback_agree_on_a_plain_tree(tree):
    root = tree({
        "src/pages/Home.tsx": "export default function Home() { return null }\n",
        "src/router.tsx": "import Home from './pages/Home'\n",
    })
    graph._cache.clear()
    parsed = graph.extract(root)
    fallback = graph._extract_regex(root)
    assert parsed["method"] == "parser"
    for key in ("router.tsx", "pages/Home.tsx"):
        p, f = parsed["modules"][key], fallback["modules"][key]
        assert p["hasDefaultExport"] == f["hasDefaultExport"]
        assert [i["spec"] for i in p["imports"]] == [i["spec"] for i in f["imports"]]


def test_extractor_output_is_deterministic(tree):
    root = tree({
        "src/b.ts": "export const b = 1\n",
        "src/a.ts": "import { b } from './b'\nexport const a = 2\n",
    })
    one = subprocess.run(["node", str(_MJS), str(root)],
                         capture_output=True, text=True).stdout
    two = subprocess.run(["node", str(_MJS), str(root)],
                         capture_output=True, text=True).stdout
    assert one == two
    assert json.loads(one)["method"] == "parser"
