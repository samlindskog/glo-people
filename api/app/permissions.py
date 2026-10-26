"""Role → permission contract, loaded from shared/permissions.json (also imported by the web app)."""

import json
from functools import lru_cache

from app.config import get_settings


@lru_cache
def load_contract() -> dict:
    with open(get_settings().permissions_file) as f:
        contract = json.load(f)
    known = set(contract["permissions"])
    for role, spec in contract["roles"].items():
        unknown = set(spec["permissions"]) - known
        if unknown:
            raise ValueError(f"role {role!r} references unknown permissions {sorted(unknown)}")
    return contract


def roles() -> dict[str, dict]:
    return load_contract()["roles"]


def permissions_for(role: str) -> frozenset[str]:
    spec = roles().get(role)
    return frozenset(spec["permissions"]) if spec else frozenset()


def is_role(role: str) -> bool:
    return role in roles()


# API keys exist to search and edit people from MCP / scripts. They can never administer
# accounts or mint further keys, even when their owner can.
API_KEY_FORBIDDEN = frozenset({"accounts:manage", "api_keys:manage_own", "api_keys:manage_all"})
