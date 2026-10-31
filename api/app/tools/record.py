"""Tool (level 2): append values to one record. The smallest tool: assistants write through it so
a plain write gets the same transaction, events and accounting as every other tool."""

from typing import Any

from app.tools import ToolContext, ToolResult


async def write(ctx: ToolContext, values: dict[str, Any], tier: str | None = None) -> ToolResult:
    """Stage `values` (None removes a value) under the context's source."""
    changes = await ctx.tx.set_values(values, ctx.source, tier=tier, tag="write")
    for c in changes:
        if c.action in ("set", "append", "removed"):
            ctx.emit("write", f"{c.key}: {c.action}", key=c.key, action=c.action)
    return ToolResult(person_id=ctx.person_id, changes=changes)
