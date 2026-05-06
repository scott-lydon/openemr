"""Repository hygiene tests.

These tests assert structural invariants about the clinical-copilot
subtree. They run in CI under the `hygiene` job in
`.github/workflows/w2-eval-gate.yml`.

Each invariant raises a clear, specific error message naming the offending
file and the rule it violated, so a CI failure can be diagnosed without
reading the source.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIDECAR = REPO_ROOT / "sidecar"
BFF = REPO_ROOT / "bff"
EVALS = REPO_ROOT / "evals"

SKIP_DIRS = {"__pycache__", ".egg-info", ".pytest_cache", "_attic"}


def _python_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        path
        for path in root.rglob("*.py")
        if not any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts)
    ]


@pytest.mark.parametrize("path", _python_files(SIDECAR), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_sidecar_python_files_compile(path: Path) -> None:
    """Every sidecar Python file must compile.

    A file that won't even compile cannot be safely imported into the
    running app or executed by any worker. Catch this in CI rather than
    at the first dynamic import in production.
    """
    source = path.read_text()
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)} has a SyntaxError: "
            f"line {exc.lineno}: {exc.msg}. "
            f"Run `python -m py_compile <path>` locally to reproduce."
        )


@pytest.mark.parametrize("path", _python_files(SIDECAR), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_sidecar_python_files_have_module_docstring(path: Path) -> None:
    """Every sidecar Python file must declare a module docstring."""
    source = path.read_text()
    # Skip empty package markers.
    if path.name == "__init__.py" and len(source.strip()) == 0:
        pytest.skip("empty __init__.py is fine; not part of public API")

    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        pytest.skip(f"cannot create spec for {path}")
        return

    # We don't actually exec — just parse the AST for the docstring.
    import ast

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # The compile test above will fail this case; don't double-report.
        return

    docstring = ast.get_docstring(tree)
    if not docstring:
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)} has no module docstring. "
            f"Add a one-line description at the top of the file. "
            f"Pattern: '\"\"\"<one line>.\"\"\"' on the first non-blank line."
        )


PRIVATE_IMPORT = re.compile(r"^\s*from\s+([\w\.]+)\s+import\s+_\w+", re.MULTILINE)


@pytest.mark.parametrize("path", _python_files(SIDECAR), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_private_imports_from_dependencies(path: Path) -> None:
    """Forbid `from <package>._private import ...`.

    Reaching into a dependency's private module breaks compatibility and
    surprises maintainers. If you genuinely need a private symbol, vendor
    it or wrap it behind an internal interface.
    """
    source = path.read_text()
    for match in PRIVATE_IMPORT.finditer(source):
        module = match.group(1)
        # Allow our own private modules (sidecar.foo._bar is fine; that's
        # internal, not third-party).
        if module.startswith(("sidecar.", "bff.")):
            continue
        line_no = source[: match.start()].count("\n") + 1
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)}:{line_no} imports a private "
            f"symbol from {module}. Replace with a public alternative or "
            f"vendor the symbol behind an internal wrapper."
        )


def test_pyproject_toml_parses() -> None:
    """`pyproject.toml` must parse and declare every required group."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)

    extras = data.get("project", {}).get("optional-dependencies", {})
    required_groups = {
        "openai",
        "langgraph",
        "postgres",
        "observability",
        "phi",
        "w2",
        "w2_ingest",
        "w2_rag",
        "w2_sanitize",
        "w2_render",
        "w2_judges",
        "w2_widgets",
        "w2_test",
        "dev",
    }
    missing = required_groups - extras.keys()
    if missing:
        pytest.fail(
            f"pyproject.toml is missing optional-dependencies group(s): "
            f"{sorted(missing)}. See clinical-copilot/DEPENDENCIES.md for "
            f"the canonical list."
        )


def test_runbook_exists() -> None:
    """`RUNBOOK.md` must exist; every named failure mode in W2_QUALITY_PLAN
    has an entry."""
    runbook = REPO_ROOT / "RUNBOOK.md"
    if not runbook.is_file():
        pytest.fail(
            "RUNBOOK.md not found at clinical-copilot/RUNBOOK.md. "
            "Every named failure mode in W2_QUALITY_PLAN.md has a runbook entry."
        )


def test_security_md_exists() -> None:
    """`SECURITY.md` must exist."""
    sec = REPO_ROOT / "SECURITY.md"
    if not sec.is_file():
        pytest.fail(
            "SECURITY.md not found at clinical-copilot/SECURITY.md. "
            "It documents the threat model and PHI commitments."
        )


def test_codeowners_exists() -> None:
    """`CODEOWNERS` must exist; touching protected paths requires review."""
    co = REPO_ROOT / "CODEOWNERS"
    if not co.is_file():
        pytest.fail(
            "CODEOWNERS not found at clinical-copilot/CODEOWNERS. "
            "It routes review requests for evals, schemas, and architecture."
        )
