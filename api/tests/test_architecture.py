"""The functionality levels (ARCHITECTURE.md) are a test: every module has a level, and a module
imports only lower levels or the listed peers at its own level."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# Level by module (a package name covers every file in it).
LEVELS: dict[str, int] = {
    # 0 — rules and adapters
    **dict.fromkeys(["fields", "history", "normalize", "property_specs", "embedder", "urls",
                     "config", "db", "mongo", "auth", "permissions", "activity", "observability",
                     "jobs", "integrations"], 0),
    # 1 — stores
    **dict.fromkeys(["properties", "people", "embeddings"], 1),
    # 2 — tools and the agent plumbing they share
    **dict.fromkeys(["tools", "agents"], 2),
    # 3 — assistants (per-person assistants, their runner, ask)
    "assistants": 3,
    # 4 — surfaces
    **dict.fromkeys(["routers", "mcp_server", "cli", "main", "importer"], 4),
}

# Imports allowed between different packages of the same level (importer → imported). Inside one
# package anything goes; levels 0, 3 and 4 are open (their modules form one system each).
PEERS: dict[str, set[str]] = {
    "people": {"properties"},
    "embeddings": {"people", "properties"},
    "tools": {"agents"},            # every tool may use the agent plumbing; agents use no tool
}
OPEN_LEVELS = {0, 3, 4}


def _system(path: Path) -> str:
    rel = path.relative_to(APP)
    return rel.parts[0] if len(rel.parts) > 1 else rel.stem


def _unit(path: Path) -> str:
    """`tools/store` for a package module, `people` for a top-level one."""
    rel = path.relative_to(APP).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return "/".join(parts[:2])


def _imports(path: Path) -> set[str]:
    """Units a file imports: `from app.tools.search import x` → {tools, tools/search}; `from
    app.tools import store` → {tools, tools/store}; `from app import people` → {people}."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module == "app":
            out.update(alias.name for alias in node.names)
            continue
        if not node.module.startswith("app."):
            continue
        parts = node.module.split(".")[1:]
        out.add(parts[0])
        if len(parts) > 1:
            out.add(f"{parts[0]}/{parts[1]}")
        else:
            out.update(f"{parts[0]}/{alias.name}" for alias in node.names)
    return out


def _allowed(src: Path, target: str) -> bool:
    system, unit = _system(src), _unit(src)
    target_system = target.split("/")[0]
    if target_system not in LEVELS:
        return True                                   # a name, not an app module
    mine, theirs = LEVELS[system], LEVELS[target_system]
    if theirs != mine:
        return theirs < mine
    if target_system == system or mine in OPEN_LEVELS:
        return True                                   # inside one package / one open level
    peers = PEERS.get(unit, set()) | PEERS.get(system, set())
    return target in peers or target_system in peers


def test_every_module_has_a_level():
    unmapped = sorted({_system(p) for p in APP.rglob("*.py")} - set(LEVELS) - {"__init__"})
    assert not unmapped, f"modules without a level: {unmapped}"


def test_imports_go_down_the_levels():
    violations = []
    for path in sorted(APP.rglob("*.py")):
        if _system(path) == "__init__":
            continue
        for target in sorted(_imports(path)):
            if not _allowed(path, target):
                violations.append(f"{path.relative_to(APP)} (level {LEVELS[_system(path)]}) "
                                  f"imports {target}")
    assert not violations, "\n".join(violations)
