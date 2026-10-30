"""GitHub account facts via the `github-com` provider (Firecrawl Alexandria), 5 credits per
profile. Only called when a GitHub URL is already known; a miss costs nothing."""

from typing import Any

from app.workflows.context import WorkflowDeps
from app.workflows.enrich.budget import GITHUB_USER_CREDITS
from app.workflows.enrich.models import GitHubProfile
from app.workflows.enrich.urls import normalize_url

AGENT_NAME = "github"
PROVIDER = "github-com"
TOP_REPOS = 5


async def lookup(deps: WorkflowDeps, github_url: str) -> GitHubProfile | None:
    res = await deps.firecrawl.alexandria(PROVIDER, "repositories/user",
                                          {"url": github_url, "top_repos": TOP_REPOS},
                                          expected_credits=GITHUB_USER_CREDITS)
    return from_record(res.data, github_url)


def from_record(data: Any, github_url: str) -> GitHubProfile | None:
    if not isinstance(data, dict) or not data.get("login"):
        return None
    user = data.get("user") or {}
    return GitHubProfile(
        url=data.get("source_url") or github_url,
        login=str(data["login"]),
        name=user.get("name") or None,
        bio=user.get("bio") or None,
        company=user.get("company") or None,
        location=user.get("location") or None,
        blog=normalize_url(user.get("blog")),
        followers=int(user.get("followers") or 0),
        public_repos=int(user.get("public_repos") or 0),
        top_repos=[f"{r.get('name')} (★{r.get('stars', 0)})" for r in data.get("top_repos") or []
                   if r.get("name")],
        languages=[str(lang.get("language")) for lang in data.get("languages") or []
                   if lang.get("language")],
    )
