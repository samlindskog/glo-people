"""GitHub account facts via the `github-com` provider on Firecrawl Alexandria, 5 credits per
profile. Only called when a GitHub URL is already known; a miss costs nothing."""

from typing import Any

from pydantic import BaseModel, Field

from app.integrations.firecrawl import AsyncFirecrawl
from app.urls import normalize_url

PROVIDER = "github-com"
USER_CREDITS = 5
TOP_REPOS = 5


class GitHubProfile(BaseModel):
    """What the GitHub data tool says about an account."""

    url: str
    login: str
    name: str | None = None
    bio: str | None = None
    company: str | None = None
    location: str | None = None
    blog: str | None = None
    followers: int = 0
    public_repos: int = 0
    top_repos: list[str] = Field(default_factory=list, description="'name (★stars)' entries")
    languages: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        bits = [b for b in (self.name, self.bio) if b]
        head = " — ".join(bits) if bits else self.login
        facts = [f"{self.public_repos} public repos", f"{self.followers} followers"]
        if self.company:
            facts.append(f"at {self.company}")
        out = f"{head}. {', '.join(facts)}."
        if self.top_repos:
            out += " Top repos: " + ", ".join(self.top_repos[:5]) + "."
        if self.languages:
            out += " Languages: " + ", ".join(self.languages[:5]) + "."
        return out


async def lookup(client: AsyncFirecrawl, github_url: str) -> GitHubProfile | None:
    res = await client.alexandria(PROVIDER, "repositories/user",
                                  {"url": github_url, "top_repos": TOP_REPOS},
                                  expected_credits=USER_CREDITS)
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
