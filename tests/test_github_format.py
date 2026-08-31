"""`--format github`: findings as GitHub Actions workflow annotations.

One annotation per line on stdout, in the `::error`/`::warning` command shape
the runner parses, so a CI failure lands on the offending line of the PR diff
instead of in a log nobody opens. The contract pinned here:

  * every rejection is one `::error` with `file=`, `line=` and the TS code as
    `title=` — the file path is composed from the root AS THE CALLER WROTE IT
    (CI passes a workspace-relative root, and the runner matches `file=`
    against workspace-relative paths; resolving it would break exactly that);
  * advisories (export-shape findings, reported-never-rejected) are
    `::warning`, same addressing;
  * message text is escaped per the runner's rules (% CR LF), property values
    also escape `,` and `:` — an unescaped newline truncates the annotation
    and swallows every one after it on the same print;
  * a clean tree emits NO annotations, just the ok line;
  * exit codes are untouched by the format: 1 on rejections, 0 clean.
"""

from __future__ import annotations

import json

from import_gate.cli import main as cli_main

BROKEN = {
    "src/pages/About.tsx": "import { gone } from './missing'\n",
    "src/main.tsx": "import App from './App'\n",
    "src/App.tsx": "export default function App() { return null }\n",
}


def test_a_rejection_is_an_error_annotation(tree, capsys, monkeypatch, tmp_path):
    root = tree(BROKEN, deps={})
    monkeypatch.chdir(tmp_path)
    assert cli_main(["ter", "--format", "github"]) == 2  # control: bad root still 2

    assert cli_main([".", "--format", "github"]) == 1
    out = capsys.readouterr().out
    ann = [ln for ln in out.splitlines() if ln.startswith("::error ")]
    assert len(ann) == 1, out
    line = ann[0]
    assert "file=src/pages/About.tsx" in line
    assert "line=1" in line
    assert "title=TS2307" in line
    assert "./missing" in line


def test_the_root_is_used_as_written_not_resolved(tree, capsys, monkeypatch, tmp_path):
    """CI calls `import-gate frontend --format github` from the workspace
    root; the annotation must say `frontend/src/...` so the runner can match
    it to the checkout."""
    sub = {f"frontend/{k}": v for k, v in BROKEN.items()}
    tree(sub)
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"dependencies": {}}))
    monkeypatch.chdir(tmp_path)
    assert cli_main(["frontend", "--format", "github"]) == 1
    out = capsys.readouterr().out
    assert any("file=frontend/src/pages/About.tsx" in ln for ln in out.splitlines()
               if ln.startswith("::error ")), out


def test_message_escaping_survives_newlines_and_percent(tree, capsys, monkeypatch, tmp_path):
    """The detail text is machine-composed today, but the escaping is pinned
    against the runner's rules, not against what details happen to contain."""
    from import_gate.cli import _github_annotation

    ann = _github_annotation("error", "src/a.tsx", 3, "TS2307",
                            "line one\nline two % raw\r\n")
    assert "\n" not in ann and "\r" not in ann
    assert "%0A" in ann and "%25" in ann and "%0D" in ann
    # property values escape their own delimiters too
    ann2 = _github_annotation("error", "src/a,b:c.tsx", 3, "TS2307", "m")
    head = ann2.split("::", 2)[1]
    assert "%2C" in head and "%3A" in head


def test_a_clean_tree_emits_no_annotations(tree, capsys, monkeypatch, tmp_path):
    tree({"src/main.tsx": "import App from './App'\n",
          "src/App.tsx": "export default function App() { return null }\n"},
         deps={})
    monkeypatch.chdir(tmp_path)
    assert cli_main([".", "--format", "github"]) == 0
    out = capsys.readouterr().out
    assert "::error" not in out and "::warning" not in out
    assert "ok" in out


def test_json_flag_still_works_as_an_alias(tree, capsys, monkeypatch, tmp_path):
    """--json predates --format; both spellings stay valid and agree."""
    tree(BROKEN, deps={})
    monkeypatch.chdir(tmp_path)
    assert cli_main([".", "--json"]) == 1
    legacy = json.loads(capsys.readouterr().out)
    assert cli_main([".", "--format", "json"]) == 1
    modern = json.loads(capsys.readouterr().out)
    assert legacy == modern
