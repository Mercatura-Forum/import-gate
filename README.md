# import-gate

**Deterministic import validation for generated TypeScript — catch TS2307
before the build runs.**

A code generator — a model, a scaffolder, a codemod — writes
`import Card from '../components/ui/Card'` into a tree that has no such file.
The pipeline then runs in full: install, bundle, type-check, sometimes a
browser probe. Minutes later, `tsc` reports
`TS2307: Cannot find module '../components/ui/Card'` — a diagnosis a string
comparison could have made before any of it started.

`import-gate` makes that comparison. No model, no compiler, no network: a
case-sensitive directory walk, a syntactic parse, and a lookup — producing
the diagnostic `tsc` would have produced (message-exact; the column is
reported as 1), plus a correction message a code generator can act on.

```
$ import-gate path/to/app
pages/About.tsx(3,1): error TS2307: Cannot find module '../components/ui/Card' or its corresponding type declarations.

Your change imports modules that do not exist. This was caught before the
build ran, so nothing has been verified yet — fix the imports and the same
change will proceed.

  pages/About.tsx:3  '../components/ui/Card' — no such file in the tree (matching is case-sensitive)

Import only modules that already exist in the tree, create the file you are
importing in the same change, or declare the dependency in package.json first.
```

## What it checks

| class | judged against | tsc code |
|---|---|---|
| relative imports (`./x`, `../ui/Card`) | the tree itself — case-sensitive, TypeScript's own extension order, directory-index fallback | TS2307 |
| bare packages (`react-hook-form`, `@scope/pkg/subpath`) | `package.json` dependencies / devDependencies / peerDependencies, by package **root** | TS2307 |
| path aliases (`@/components/…`, `~/…`) | `tsconfig.json` `compilerOptions.paths` (JSONC tolerated) | TS2307 |
| subpath imports (`#…`) | `package.json` `imports` field | TS2307 |
| export shape (default import of a named export, and vice versa) | the target module's parsed exports — **advisory only**, never rejected on | TS2613 / TS2305 |

And what it deliberately lets through, because each would be a false
positive under a naive rule: Node builtins (`path`, `node:crypto` — the
bundler resolves them without a package.json entry), asset imports
(`./logo.svg`, `./style.css?raw` — bundler query suffixes are not part of
the path), virtual modules (`virtual:…`), aliases that **do** have a
`paths` mapping, scoped package roots (`@hookform/resolvers/zod` belongs to
`@hookform/resolvers`), typings supplied by a `@types/*` devDependency, and
a package importing itself by its own `name`.

## The design rule everything follows

**Every ambiguity resolves to "accept".** A false negative costs what you
already pay today — one wasted build. A false positive rejects a legitimate
change, and inside a generate-verify-retry loop it can spin a change that
was fine. So whenever absence cannot be PROVEN, the gate
**fails open**: an unreadable tree (`method`/`errored` on the report say so,
loudly), an unreadable or dependency-less `package.json` (the package half
stops judging), an unparseable `tsconfig.json` or one that `extends` a base
config while defining no local `paths` (the alias half stops judging), an
unreadable `imports` field (the subpath half stops judging), and a change
set that contains no TypeScript at all (nothing to judge — a stylesheet
edit is never rejected for a pre-existing break elsewhere). A gate that
stopped gating is visible in the report instead of looking like a clean
tree.

## Install & use

```
pip install .
```

CLI — exit 0 when every import resolves, 1 on rejections:

```
import-gate path/to/app                        # whole tree
import-gate path/to/app --changed src/pages/About.tsx
import-gate path/to/app --json                 # the full report
import-gate path/to/app --facts                # the contract, prompt-ready
```

Library — the shape a generate-verify loop wants:

```python
from import_gate import gate, repair, check, facts

report = gate("path/to/app", changed=changed_files)
if not report.ok:
    reprompt(report.retry_text())      # names every offender, file:line
    log(report.gate_detail())          # the same facts in tsc's own shape
    log(report.as_record())            # JSON-able, stable field set
```

`root` is the package root — the directory holding `src/`, `package.json`
and `tsconfig.json`.

### As a GitHub Action

The same gate as a CI step, with every finding annotated onto the offending
line of the pull-request diff (`--format github` under the hood):

```yaml
- uses: Mercatura-Forum/import-gate@main
  with:
    root: frontend            # workspace-relative package root
    # changed: src/pages/About.tsx src/router.tsx   # optional scoping
```

Rejections become `error` annotations (the TS code as the title), export-shape
advisories become `warning`s, and the step fails exactly when the CLI would
exit 1. Pass `root` relative to the workspace — the annotations reuse it as
written, which is how the runner matches them to files in the diff. The action
installs nothing from a registry: it runs the checked-out revision you pinned.

### Scoping to a change

`gate(root, changed=[...])` restricts the check to the files a change
touched — with two deliberate asymmetries:

* the **relative** half widens the examined set with every module that
  imports a changed file, because editing (or deleting!) a page breaks the
  *importer's* line, and the importer is never in the edit set. Deletions
  are found by the specifier's would-be target, since a deleted module no
  longer resolves;
* the **package/alias** halves examine only the changed files, because a
  package specifier's validity depends on `package.json` alone — widening
  would let a pre-existing break elsewhere reject an unrelated change.

### The additive repair

Export-shape mismatches (TS2613/TS2305) are often mechanically provable:

```python
report = check(root)      # the contract check (relative imports + shapes)
repair(root, report)      # ADDITIVE only — see below
```

`repair` appends the missing export form when — and only when — the binding
provably already exists in the target under the other form
(`export default X` for a default import of the named export `X`;
`export { X }` for a named import of the named default declaration `X`). It
never deletes, never rewrites, never touches an importer. Anything else — an
anonymous default, a name mismatch, an unresolved module — is left for the
author, with the reason recorded.

### Prompt facts

`facts(root)` renders the page↔router export contract as a short, complete
block of text to paste into a code generator's prompt, so the generator
keeps the contract instead of learning it from a compile error:

```
### Page ↔ router module contract (machine-read from this tree — COMPLETE)
The `export` in a page and the `import` in router.tsx are ONE contract. …
- 9 pages are DEFAULT imports (`import X from './pages/X'`) and must keep `export default`.
- EXCEPT these 2, which router.tsx imports BY NAME …
```

## How the parsing works

A small Node script (`extract.mjs`) parses every module with the TypeScript
compiler API — a pure **syntactic** parse (`ts.createSourceFile`): no
type-check, no `node_modules` needed for the tree under inspection, and
byte-identical output for a byte-identical tree. It prefers the target
tree's own `typescript`, then any `typescript` resolvable from the package's
location — and it verifies the module actually exposes the **parser API**
before trusting it, because a `typescript` v7+ installation resolves
successfully while shipping no JS compiler API at all.

When Node or a usable `typescript` is unavailable — or when the parser
errors on **most** of a tree, which is a toolchain statement, not a code
statement — a conservative regex extractor takes over and marks the report
`method: "regex-fallback"`, so a weaker reading is never mistaken for the
parser's. Modules the extractor could not read are counted in `errored`:
"0 violations over 50 modules, 50 errored" is not the same statement as
"0 violations over 50 modules", and the report never lets the two look alike.

## What this is not

* Not a type checker — it proves imports resolve and export shapes match,
  nothing about the types themselves. Run `tsc` after; this gate exists so
  most `tsc` runs stop dying on line 1.
* Not a dependency resolver — it checks a package is *declared*, not that
  the declared version exists, installs, or exports what you import from it.
* Not a linter — a clean pass means "nothing here that cannot resolve",
  not "good code".

## License

Apache-2.0.
