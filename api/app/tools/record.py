"""Tool (level 2): append values to one record. The smallest tool: assistants write through it so
a plain write gets the same transaction, events and accounting as every other tool."""

from collections.abc import Iterable
from typing import Any

from app.tools import ToolContext, ToolResult


async def write(ctx: ToolContext, values: dict[str, Any], tier: str | None = None,
                append: Iterable[str] = ()) -> ToolResult:
    """Stage `values` (None removes a value) under the context's source; keys in `append` are
    merged into the current value (list items unioned, a text added as a line)."""
    changes = await ctx.stage(values, append=append, tier=tier)
    return ToolResult(person_id=ctx.person_id, changes=changes)
