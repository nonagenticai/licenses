"""Source-level invariant: every third-party module the SHIPPED code imports must be a
DECLARED runtime dependency — not merely a transitive that happens to be installed.

THE DEFECT THIS EXISTS FOR
--------------------------
`auth/middleware.py:20` does `import httpx`. `httpx` was declared **only** in the `dev`
dependency-group, never in `[project].dependencies`. It reached the production image purely
as a transitive of `python-keycloak`.

PR #140 deleted `auth/validator.py` — the sole importer of `python-keycloak` — and dropped that
dependency. `uv lock` correctly removed it *and its transitives*, `httpx` among them. The
production image then had no `httpx`, and the pod **CrashLoopBackOff**'d at import:

    File "/app/auth/middleware.py", line 20, in <module>
        import httpx
    ModuleNotFoundError: No module named 'httpx'

**Every gate was green.** 1333 unit tests passed, the integration suite passed, `ruff`, `ty`,
`pip-audit`, `detect-secrets`, and CI's own `Build, push, smoke` all passed — because the test
environment installs the `dev` group, where `httpx` *was* declared. The runtime-only install that
production performs was never exercised anywhere. Only the rolling update caught it, and only
because Kubernetes refused to retire the old ReplicaSet for a pod that never became Ready.

The same audit found `anyio` in the identical state (a direct import, satisfied only via
`starlette`). It had not broken yet. A runtime dependency that is only ever a transitive is one
unrelated removal away from this.

THE RULE
--------
Collect every top-level third-party module imported anywhere in the shipped tree, map it to its
distribution name, and require that distribution to appear in `[project].dependencies`.

Import name != distribution name (`jwt` ships in `pyjwt`), so the mapping is explicit rather than
guessed. An unmapped third-party import fails the test by name — that is deliberate: the next
person to add a dependency must state which distribution provides it, which is exactly the step
that was skipped here.

WHAT THIS RULE DOES **NOT** CATCH (stated, not implied)
-------------------------------------------------------
  1. **A version floor that is too low.** It proves the distribution is declared, not that the
     declared range contains the API in use.
  2. **Imports inside `try:`/`except ImportError:`.** An optional dependency reads the same as a
     required one; there are none in this tree today.
  3. **Dynamic imports** — `importlib.import_module(name)` where `name` is computed.
  4. **The reverse direction:** a declared dependency nothing imports. Harmless, and `pip-audit`
     already has an opinion about it.

The complete check is `uv sync --no-dev && python -c "import main"`, which reproduces the
production install exactly and took ~10 seconds locally. That belongs in CI; this test is the
part that can live in the repo's own suite.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SKIP_DIRS = frozenset(
    {"tests", "scripts", "migrations", ".venv", ".worktrees", "certs", "docs", "__pycache__"}
)

#: Import name -> distribution name, for the cases where they differ.
_IMPORT_TO_DIST: dict[str, str] = {
    "jwt": "pyjwt",
    "psycopg": "psycopg",
    "multipart": "python-multipart",
    "pythonjsonlogger": "python-json-logger",
    "pydantic_settings": "pydantic-settings",
    "fastapi_cors": "fastapi-cors",
    "dateutil": "python-dateutil",
    "yaml": "pyyaml",
}


#: Import names that are neither stdlib nor third-party: this repo's own top-level modules
#: and package directories. Derived, not hard-coded, so a new local module cannot trip the rule.
def _local_names() -> set[str]:
    names = {p.stem for p in REPO_ROOT.glob("*.py")}
    names |= {
        d.name
        for d in REPO_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".") and (d / "__init__.py").exists()
    }
    # `scripts/` is importable as a namespace package from the repo root even without
    # __init__.py, and is excluded from the shipped corpus above.
    names.add("scripts")
    return names


def _shipped_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    )


def _third_party_imports() -> dict[str, list[str]]:
    """module -> the shipped files that import it."""
    local = _local_names()
    std = set(sys.stdlib_module_names)
    found: dict[str, list[str]] = {}
    for path in _shipped_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in std or top in local:
                    continue
                found.setdefault(top, [])
                if rel not in found[top]:
                    found[top].append(rel)
    return found


def _declared_runtime_dists() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dists = set()
    for spec in data["project"]["dependencies"]:
        # "psycopg[binary,pool]>=3.2.0" -> "psycopg"
        name = re.split(r"[\[<>=!~;\s]", spec, maxsplit=1)[0].strip().lower().replace("_", "-")
        if name:
            dists.add(name)
    return dists


_IMPORTS = _third_party_imports()
_DECLARED = _declared_runtime_dists()


class TestTheCorpusIsReal:
    """Anti-vacuity. An empty import set would make the rule below hold for free."""

    def test_shipped_files_were_found(self):
        files = _shipped_files()
        assert len(files) >= 5, f"only {len(files)} shipped files found; the walk is broken"
        rels = {p.relative_to(REPO_ROOT).as_posix() for p in files}
        for required in ("main.py", "models.py", "auth/middleware.py"):
            assert required in rels, f"{required} missing from the corpus"

    def test_third_party_imports_were_found(self):
        assert len(_IMPORTS) >= 5, (
            f"only {len(_IMPORTS)} third-party imports detected ({sorted(_IMPORTS)}); "
            f"the classifier is treating real dependencies as stdlib or local"
        )

    def test_the_defect_module_is_still_imported(self):
        """If this fails, `httpx` left the codebase and this rule lost its subject."""
        assert "httpx" in _IMPORTS, (
            "httpx is no longer imported by shipped code — if that is intentional, drop it from "
            "[project].dependencies too, and update this test"
        )

    def test_declared_runtime_dependencies_were_parsed(self):
        assert len(_DECLARED) > 5, f"parsed {_DECLARED!r} from pyproject; that is wrong"


class TestEveryShippedImportIsDeclared:
    """THE RULE."""

    def test_every_third_party_import_maps_to_a_declared_distribution(self):
        undeclared: list[str] = []
        unmapped: list[str] = []
        for module, files in sorted(_IMPORTS.items()):
            dist = _IMPORT_TO_DIST.get(module, module.lower().replace("_", "-"))
            if dist in _DECLARED:
                continue
            if module in _IMPORT_TO_DIST:
                undeclared.append(f"{module} (dist {dist!r}) imported by {files}")
            else:
                unmapped.append(f"{module} -> guessed dist {dist!r}, imported by {files}")

        assert not undeclared, (
            "shipped code imports modules whose distribution is NOT in "
            f"[project].dependencies: {undeclared}"
        )
        assert not unmapped, (
            "shipped code imports third-party modules that are not declared runtime "
            f"dependencies: {unmapped}. If the import name differs from the distribution "
            "name, add it to _IMPORT_TO_DIST. Do NOT rely on it arriving as a transitive — "
            "that is the defect this module exists for."
        )
