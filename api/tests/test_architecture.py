"""The dependency direction between systems (see the plan's architecture rules) is a test."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Lower layers must not import higher ones. Each entry lists modules a module may NOT import.
FORBIDDEN = {
    "people": {"embeddings", "search", "agents", "workflows", "importer"},
    "properties": {"people", "embeddings", "search", "agents", "workflows", "importer", "normalize"},
    "normalize": {"people", "embeddings", "search", "agents", "workflows", "importer"},
    "mongo": {"properties", "people", "embeddings", "search", "agents", "workflows", "normalize"},
    "property_specs": {"properties", "people", "embeddings", "search", "agents", "workflows",
                       "fields", "history", "mongo", "db", "config"},
    "history": {"people", "embeddings", "search", "agents", "workflows", "properties", "mongo"},
    "fields": {"people", "embeddings", "search", "agents", "workflows", "properties"},
    "embedder": {"people", "embeddings", "search", "agents", "workflows", "fields", "history"},
    "embeddings": {"search", "agents", "workflows"},
    "search": {"agents", "workflows"},
    "integrations": {"agents", "workflows", "people", "search"},
    "agents": {"workflows", "routers", "mcp_server"},
    "workflows/runner": {"integrations", "agents"},
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
            parts = node.module.split(".")
            out.add(parts[1])
            if len(parts) > 2:
                out.add(f"{parts[1]}/{parts[2]}")
            for alias in node.names:                     # `from app import people`
                if parts == ["app"]:
                    out.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "app":
            for alias in node.names:
                out.add(alias.name)
    return out


def _files(name: str) -> list[Path]:
    p = APP / name
    if p.is_dir():
        return [f for f in p.rglob("*.py")]
    if (APP / f"{name}.py").exists():
        return [APP / f"{name}.py"]
    return [APP / f"{name}.py"] if name.endswith(".py") else [APP / (name + ".py")]


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
