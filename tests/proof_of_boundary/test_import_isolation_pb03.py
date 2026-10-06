# PB-03 — Import Isolation
#
# Verifies that no file under src/ imports from the platform SDK.
#
# docs/03_test_spec.md §PB-03:
#   Method:   AST scan of all .py files under src/
#   Expected: Zero violations — no "import agenticstar" or "from agenticstar"
#
# This is the PB-03 companion to the existing test_import_isolation.py (PB-4).
# PB-03 is named per docs/03_test_spec.md; it scans only for "agenticstar"
# (the platform SDK package). The companion PB-4 test also scans for "platform"
# (which includes agenticstar.platform); this test is narrower and definitional.

import ast
import os
import pytest


_PROHIBITED_PREFIX = "agenticstar"


def _find_py_files(directory: str):
    """Yield all .py file paths under directory (recursive)."""
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            if fname.endswith(".py"):
                yield os.path.join(root, fname)


def _scan_agenticstar_imports(filepath: str):
    """Return list of violation strings for agenticstar imports in filepath."""
    with open(filepath, encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=filepath)
        except SyntaxError as exc:
            return [f"{filepath}: SyntaxError — {exc}"]

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _PROHIBITED_PREFIX or alias.name.startswith(f"{_PROHIBITED_PREFIX}."):
                    violations.append(f"{filepath}:{node.lineno} — import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == _PROHIBITED_PREFIX or node.module.startswith(f"{_PROHIBITED_PREFIX}.")):
                violations.append(f"{filepath}:{node.lineno} — from {node.module} import ...")
    return violations


class TestImportIsolationPB03:
    """PB-03: src/ must not import the platform SDK at all."""

    def test_no_agenticstar_imports_in_src(self):
        """All .py files under src/ must not import agenticstar or agenticstar.*."""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        src_dir = os.path.join(repo_root, "src")

        if not os.path.exists(src_dir):
            pytest.skip(f"src/ directory not found at {src_dir}")

        violations = []
        for filepath in _find_py_files(src_dir):
            violations.extend(_scan_agenticstar_imports(filepath))

        assert violations == [], "PB-03 FAIL — platform SDK import violations found:\n" + "\n".join(violations)

    def test_graph_files_are_framework_only(self):
        """graph.py and domain_workflow_graph.py must import only from 'framework' or 'src'."""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        graph_dir = os.path.join(repo_root, "src", "graph")

        if not os.path.exists(graph_dir):
            pytest.skip(f"src/graph/ not found at {graph_dir}")

        violations = []
        for filepath in _find_py_files(graph_dir):
            violations.extend(_scan_agenticstar_imports(filepath))

        assert violations == [], "PB-03 FAIL — agenticstar import violations in src/graph/:\n" + "\n".join(violations)
