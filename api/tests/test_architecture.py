"""The dependency direction between systems (see the plan's architecture rules) is a test."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Lower layers must not import higher ones. Each entry lists modules a module may NOT import; a
# key may name a package (every file in it) or one file. The search and normalize packages hold
# two layers each: the engine / parser below the agents, the operations (ask, store, person)
# above them.
OPERATIONS = {"search/ask", "search/store", "normalize/person"}
FORBIDDEN = {
    "people": {"embeddings", "search", "agents", "workflows", "importer", *OPERATIONS},
    "properties": {"people", "embeddings", "search", "agents", "workflows", "importer", "normalize"},
    "normalize/__init__.py": {"people", "embeddings", "search", "agents", "workflows", "importer"},
    "normalize/person.py": {"workflows", "routers", "mcp_server", "search"},
    "mongo": {"properties", "people", "embeddings", "search", "agents", "workflows", "normalize"},
    "property_specs": {"properties", "people", "embeddings", "search", "agents", "workflows",
                       "fields", "history", "mongo", "db", "config"},
    "history": {"people", "embeddings", "search", "agents", "workflows", "properties", "mongo"},
    "fields": {"people", "embeddings", "search", "agents", "workflows", "properties"},
    "embedder": {"people", "embeddings", "search", "agents", "workflows", "fields", "history"},
    "embeddings": {"search", "agents", "workflows"},
    "search/__init__.py": {"agents", "workflows", *OPERATIONS},
    "search/engine.py": {"agents", "workflows", *OPERATIONS},
    "search/ask.py": {"workflows", "routers", "mcp_server", "search/store"},
    "search/store.py": {"workflows", "routers", "mcp_server", "search/ask"},
    "integrations": {"agents", "workflows", "people", "search"},
    "agents": {"workflows", "routers", "mcp_server", *OPERATIONS},
    "workflows/runner": {"integrations", "agents"},
}


def _imports(path: Path) -> set[str]:
    """Modules a file imports, as `system` and `system/module` names: `from app.search.engine
    import x` gives {search, search/engine}; `from app.search import ask` gives {search,
    search/ask} (names that are not submodules are harmless extras)."""
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module == "app":
            out.update(alias.name for alias in node.names)
            continue
        if not node.module.startswith("app."):
            continue
        parts = node.module.split(".")
        out.add(parts[1])
        if len(parts) > 2:
            out.add(f"{parts[1]}/{parts[2]}")
        else:
            out.update(f"{parts[1]}/{alias.name}" for alias in node.names)
    return out


def _files(name: str) -> list[Path]:
    p = APP / name
    if p.is_dir():
        return list(p.rglob("*.py"))
    return [p if name.endswith(".py") else APP / f"{name}.py"]


def test_dependency_direction():
    violations = []
    for module, forbidden in FORBIDDEN.items():
        for path in _files(module):
            if not path.exists():
                continue
            bad = _imports(path) & forbidden
            if bad:
                violations.append(f"{path.relative_to(APP)} imports {sorted(bad)}")
    assert not violations, "\n".join(violations)


def test_rules_name_existing_modules():
    missing = [name for name in FORBIDDEN if not any(f.exists() for f in _files(name))]
    assert not missing, missing
