"""Options of the enrichment workflow."""

from pydantic import BaseModel, Field


class EnrichOptions(BaseModel):
    refresh: bool = Field(False, description="Re-run for people enriched recently")
    max_credits: int = Field(1500, ge=1, le=100_000,
                             description="Firecrawl credit cap for the whole run")
