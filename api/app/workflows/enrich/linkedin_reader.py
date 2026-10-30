"""Agent: turn a LinkedIn data-provider record (markdown) into a structured profile."""

from pydantic_ai import Agent

from app.workflows.enrich.agents_common import excerpt, run_agent
from app.workflows.enrich.models import LinkedInProfile

AGENT_NAME = "linkedin_reader"

agent = Agent(
    name=f"enrich.{AGENT_NAME}",
    output_type=LinkedInProfile,
    instructions="""\
You read a LinkedIn profile record as returned by a data provider (markdown, layout varies by
provider) and fill in the structured profile.

Rules:
- Copy the headline verbatim. Write the summary in two or three neutral sentences.
- current_company is the most recent position with no end date ("Present"). If the record lists
  several current roles, pick the one the headline refers to. Leave it None when unclear.
- personal_sites are the person's own websites (portfolio, blog, personal domain). Do not put the
  employer's site, LinkedIn, Twitter/X, GitHub or other social networks there.
- github_url only if a github.com/<user> link appears in the record.
- Never invent URLs; only copy ones that appear in the record.""",
)


async def read(markdown: str) -> LinkedInProfile:
    return await run_agent(agent, "LinkedIn record:\n\n" + excerpt(markdown))
