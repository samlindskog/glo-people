"""Options of the enrichment assistant."""

from typing import Literal

from pydantic import BaseModel, Field

ProviderKey = Literal["fullenrich"]      # grows with each provider in enrich/providers.py


class EnrichOptions(BaseModel):
    refresh: bool = Field(False, description="Re-run for people enriched recently")
    providers: list[ProviderKey] = Field(
        ["fullenrich"], description="Data providers asked about the LinkedIn URL, in order; the "
                                    "first match fills the profile, later matches add facts")
    max_credits: int = Field(1500, ge=1, le=100_000,
                             description="Firecrawl credit cap for the whole run")
    max_new_properties: int = Field(0, ge=0, le=5,
                                    description="Properties the storage agent may create per "
                                                "person for facts the catalog has no home for; "
                                                "at 0 such facts are dropped and reported")
