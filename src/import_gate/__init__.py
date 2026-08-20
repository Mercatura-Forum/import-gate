"""import-gate — deterministic import validation for generated TypeScript.

Reject an import that cannot resolve BEFORE the expensive build runs. No
model, no compiler, no network: a directory walk, a syntactic parse and a
string comparison, reporting the exact diagnostic `tsc` would have produced
minutes later.

    from import_gate import gate

    report = gate("path/to/app", changed=["src/pages/About.tsx"])
    if not report.ok:
        print(report.retry_text())     # prose a code generator can act on
        print(report.gate_detail())    # the same facts in tsc's own shape

See the README for the design rules — above all: every ambiguity resolves to
"accept", because in a retry loop a false positive costs more than the wasted
build a false negative lets through.
"""

from .contract import (            # noqa: F401
    ContractReport,
    Violation,
    check,
    facts,
    import_form_for,
    repair,
)
from .gate import GateReport, Rejection, gate  # noqa: F401
from .graph import extract, is_relative, join_rel, resolve  # noqa: F401

__version__ = "0.1.0"

__all__ = [
    "gate", "GateReport", "Rejection",
    "check", "repair", "facts", "import_form_for",
    "ContractReport", "Violation",
    "extract", "resolve", "join_rel", "is_relative",
    "__version__",
]
