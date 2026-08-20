// extract.mjs — the import/export graph of a TypeScript tree.
//
// Usage: node extract.mjs <package_root>   (the directory holding src/)
// Prints ONE JSON object to stdout. Never reads the clock, never randomizes,
// sorts every list — byte-identical output for a byte-identical tree.
//
// Parsing is SYNTACTIC (ts.createSourceFile), not a type-check: an import
// clause and an export modifier are declared structure, so a pure parse is
// exact for this question and works on a tree whose node_modules may be
// absent. Resolution order for the parser itself: the TARGET tree's own
// `typescript` first, then whatever resolves from this file's location.
//
// Output shape:
//   { method: "parser",
//     modules: { "<rel path from src>": {
//        hasDefaultExport: bool,
//        defaultExportName: string|null,   // `export default function X`/`export default X`
//        namedExports: [string],
//        starReexports: [string],          // `export * from './x'` — makes the
//                                          // named-export set OPEN, so callers
//                                          // must not claim absence
//        imports: [{ spec, line, default: string|null, named: [string],
//                    namespace: string|null, typeOnly: bool,
//                    sideEffectOnly: bool }]
//     } } }

import { createRequire } from 'node:module';
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const root = process.argv[2];
if (!root) {
  console.error('usage: node extract.mjs <package_root>');
  process.exit(2);
}

// A resolved `typescript` is NOT the same thing as a usable parser.
// typescript@7 is the native (Go) port: its npm package ships a launcher for
// the `tsc` binary and exports exactly { version, versionMajorMinor } — no JS
// compiler API at all, and no lib/typescript.js. Requiring it SUCCEEDS, so a
// resolution-only check passes, and every later createSourceFile call throws.
// Per-file that reads as "this module failed", tree-wide it reads as "parsed
// fine, found nothing" — a gate that stopped gating while reporting health.
// So the capability, not the resolution, is what decides a candidate, and a
// candidate without the API is skipped rather than accepted.
const hasParserApi = (m) =>
  !!m && typeof m.createSourceFile === 'function'
  && !!m.ScriptTarget && !!m.ScriptKind
  && typeof m.getCombinedModifierFlags === 'function';

let ts = null;
let apiless = null;   // a typescript that resolved but exposes no parser API
for (const c of [join(root, 'node_modules', 'typescript', 'lib', 'typescript.js'),
                 'typescript']) {
  let m;
  try { m = createRequire(import.meta.url)(c); } catch { continue; }
  if (hasParserApi(m)) { ts = m; break; }
  apiless = (m && m.version) || 'unknown';
}
if (!ts) {
  console.log(JSON.stringify({
    error: 'typescript-parser-api-unavailable',
    detail: apiless
      ? `typescript ${apiless} exposes no JS compiler API (v7+ ships the `
        + `native compiler); install a 5.x for the parser`
      : 'no typescript module could be resolved',
  }));
  process.exit(0);
}

const src = join(root, 'src');

function walk(dir, out) {
  let entries;
  try { entries = readdirSync(dir).sort(); } catch { return out; }
  for (const name of entries) {
    if (name === 'node_modules' || name.startsWith('.')) continue;
    const p = join(dir, name);
    let st;
    try { st = statSync(p); } catch { continue; }
    if (st.isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

function rel(p) { return relative(src, p).split(sep).join('/'); }

function analyse(file) {
  const text = readFileSync(file, 'utf8');
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const mod = {
    hasDefaultExport: false, defaultExportName: null,
    namedExports: [], starReexports: [], imports: [],
  };
  const named = new Set();

  const line = (node) =>
    sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  const isExported = (n) =>
    (ts.getCombinedModifierFlags(n) & ts.ModifierFlags.Export) !== 0;
  const isDefault = (n) =>
    (ts.getCombinedModifierFlags(n) & ts.ModifierFlags.Default) !== 0;

  for (const st of sf.statements) {
    // ── imports ──
    if (ts.isImportDeclaration(st)) {
      const spec = st.moduleSpecifier && ts.isStringLiteral(st.moduleSpecifier)
        ? st.moduleSpecifier.text : null;
      if (spec === null) continue;
      const rec = {
        spec, line: line(st), default: null, named: [], namespace: null,
        typeOnly: !!(st.importClause && st.importClause.isTypeOnly),
        sideEffectOnly: !st.importClause,
      };
      const clause = st.importClause;
      if (clause) {
        if (clause.name) rec.default = clause.name.text;
        const b = clause.namedBindings;
        if (b && ts.isNamespaceImport(b)) rec.namespace = b.name.text;
        if (b && ts.isNamedImports(b)) {
          for (const el of b.elements) {
            // `import { A as B }` — the CONTRACT is on A, the exported name
            rec.named.push((el.propertyName || el.name).text);
          }
          rec.named.sort();
        }
      }
      mod.imports.push(rec);
      continue;
    }
    // ── export * from './x' ──
    if (ts.isExportDeclaration(st) && !st.exportClause && st.moduleSpecifier) {
      if (ts.isStringLiteral(st.moduleSpecifier)) {
        mod.starReexports.push(st.moduleSpecifier.text);
      }
      continue;
    }
    // ── export { A, B } / export { A } from './x' ──
    if (ts.isExportDeclaration(st) && st.exportClause
        && ts.isNamedExports(st.exportClause)) {
      for (const el of st.exportClause.elements) {
        if (el.name.text === 'default') mod.hasDefaultExport = true;
        else named.add(el.name.text);
      }
      continue;
    }
    // ── export default … ──
    if (ts.isExportAssignment(st)) {
      mod.hasDefaultExport = true;
      if (st.expression && ts.isIdentifier(st.expression)) {
        mod.defaultExportName = st.expression.text;
      }
      continue;
    }
    // ── export [default] function/class ──
    if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st))
        && isExported(st)) {
      if (isDefault(st)) {
        mod.hasDefaultExport = true;
        if (st.name) mod.defaultExportName = st.name.text;
      } else if (st.name) {
        named.add(st.name.text);
      }
      continue;
    }
    // ── export const/let/var ──
    if (ts.isVariableStatement(st) && isExported(st)) {
      for (const d of st.declarationList.declarations) {
        if (ts.isIdentifier(d.name)) named.add(d.name.text);
        else if (ts.isObjectBindingPattern(d.name) || ts.isArrayBindingPattern(d.name)) {
          for (const el of d.name.elements) {
            if (el.name && ts.isIdentifier(el.name)) named.add(el.name.text);
          }
        }
      }
      continue;
    }
    // ── export interface/type/enum ── (type-space, still a named export)
    if ((ts.isInterfaceDeclaration(st) || ts.isTypeAliasDeclaration(st)
         || ts.isEnumDeclaration(st)) && isExported(st) && st.name) {
      named.add(st.name.text);
    }
  }
  mod.namedExports = [...named].sort();
  mod.starReexports.sort();
  return mod;
}

const modules = {};
for (const f of walk(src, [])) {
  try { modules[rel(f)] = analyse(f); } catch (e) {
    modules[rel(f)] = { error: String(e && e.message || e) };
  }
}
const sorted = {};
for (const k of Object.keys(modules).sort()) sorted[k] = modules[k];
console.log(JSON.stringify({ method: 'parser', modules: sorted }));
