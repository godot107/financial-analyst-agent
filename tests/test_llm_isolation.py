"""Only llm.py, graph.py, __main__.py and server.py may talk to Claude.

Everything else (fetching facts, computing ratios, checking and rendering the memo) must work
without a model. That is what guarantees no number in the memo came from one.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "fin_analyst"
# server.py builds the real analyst for the HTTP service, as __main__ does for the CLI.
ALLOWED = {"llm.py", "graph.py", "__main__.py", "server.py"}
FORBIDDEN = ("anthropic", "fin_analyst.llm")


def imported_names(source: str) -> set[str]:
    """Every module a file imports, spelled as absolute names (`from .llm import x` → fin_analyst.llm)."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside the package
                module = "fin_analyst" + (f".{node.module}" if node.module else "")
            else:
                module = node.module
            names.add(module)
            # `from fin_analyst import llm` imports the llm module too
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def forbidden_imports(source: str) -> set[str]:
    return {
        name
        for name in imported_names(source)
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN)
    }


def test_only_allowed_files_import_claude():
    checked = [path for path in PACKAGE.rglob("*.py") if path.name not in ALLOWED]
    assert checked, "no package files found to check"

    offenders = {
        str(path.relative_to(PACKAGE)): sorted(bad)
        for path in checked
        if (bad := forbidden_imports(path.read_text()))
    }
    assert not offenders, f"these files must not import Claude code: {offenders}"


def test_checker_catches_each_import_style():
    # Guards the guard: if this fails, the test above could pass while missing real imports.
    assert forbidden_imports("import anthropic")
    assert forbidden_imports("from anthropic.types import Message")
    assert forbidden_imports("from fin_analyst.llm import call_claude")
    assert forbidden_imports("from fin_analyst import llm")
    assert forbidden_imports("from .llm import call_claude")
    assert forbidden_imports("from . import llm")
    assert not forbidden_imports("import pandas\nfrom fin_analyst.metrics import compute")
