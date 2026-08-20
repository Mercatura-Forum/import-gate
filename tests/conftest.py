"""Tree-builder helpers shared by every test module.

Tests run against the regex extractor by default (no Node required), which is
the honest floor: anything the fallback can prove, the parser proves too. The
parser path has its own opt-in module (test_parser_path.py) that skips itself
when Node + a usable `typescript` are absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def tree(tmp_path: Path):
    """Write a project tree from a {relpath: content} mapping; returns root."""

    def _make(files: dict[str, str], deps: dict | None = None,
              tsconfig: dict | str | None = None, name: str = "") -> Path:
        root = tmp_path
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if deps is not None:
            pj: dict = {"dependencies": deps}
            if name:
                pj["name"] = name
            (root / "package.json").write_text(json.dumps(pj))
        if tsconfig is not None:
            body = (tsconfig if isinstance(tsconfig, str)
                    else json.dumps(tsconfig))
            (root / "tsconfig.json").write_text(body)
        return root

    return _make


@pytest.fixture(autouse=True)
def _no_node(monkeypatch, request):
    """Force the regex extractor so results don't depend on the host having
    Node + typescript. The parser-path module opts out with its marker."""
    if request.node.get_closest_marker("parser_path"):
        return
    from import_gate import graph

    monkeypatch.setattr(graph, "_extract_node", lambda _root: None)
    graph._cache.clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "parser_path: exercises the real Node/typescript extractor")
