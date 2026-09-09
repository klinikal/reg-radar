"""Regression test for the "ModuleNotFoundError inside the container" class
of bug: a Dockerfile must COPY every top-level package that regradar/
actually imports, or `regradar --help` etc. crash at import time inside
that image.
"""

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOP_LEVEL_PACKAGES = {"collector", "mcpserver", "webui", "regradar"}

# Dockerfile -> the top-level packages it must COPY, given what regradar/
# imports at runtime.
DOCKERFILES = ["Dockerfile.collector", "Dockerfile.mcp", "Dockerfile.webui"]


def _imported_top_level_packages(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in TOP_LEVEL_PACKAGES:
                    found.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                top = node.module.split(".")[0]
                if top in TOP_LEVEL_PACKAGES:
                    found.add(top)
    return found


def _required_packages() -> set[str]:
    required = {"regradar"}
    for py_file in (REPO_ROOT / "regradar").rglob("*.py"):
        required |= _imported_top_level_packages(py_file)
    return required


def _copied_packages(dockerfile: Path) -> set[str]:
    copied = set()
    for line in dockerfile.read_text().splitlines():
        m = re.match(r"\s*COPY\s+([\w./-]+)/\s+\./([\w./-]+)/\s*$", line)
        if m:
            copied.add(m.group(1).strip("./"))
    return copied


def test_every_dockerfile_copies_every_package_regradar_imports():
    required = _required_packages()
    for name in DOCKERFILES:
        copied = _copied_packages(REPO_ROOT / name)
        missing = required - copied
        assert not missing, (
            f"{name} is missing COPY for {sorted(missing)} but regradar/ "
            f"imports from them (would crash with ModuleNotFoundError "
            f"inside the container)"
        )
