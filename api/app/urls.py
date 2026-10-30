"""URL helpers for the enrichment workflow: normalisation, equality keys, host classification."""

import re
from urllib.parse import urlsplit, urlunsplit

_LINKEDIN_IN = re.compile(r"^(?:[a-z]{2,3}\.)?linkedin\.com$", re.IGNORECASE)
_GITHUB_USER = re.compile(r"^/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/?$")
_GITHUB_RESERVED = {"features", "topics", "trending", "marketplace", "pricing", "login", "join",
                    "about", "explore", "sponsors", "orgs", "settings", "apps", "collections",
                    "events", "security", "enterprise", "team", "customer-stories", "site"}
SOCIAL_HOSTS = ("linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
                "tiktok.com", "youtube.com", "threads.net", "medium.com", "github.com",
                "crunchbase.com", "angel.co", "wellfound.com", "calendly.com", "t.me",
                "wa.me", "mailto:")
NON_HTML = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|zip|png|jpe?g|gif|svg|mp4|mp3)$", re.IGNORECASE)


def normalize_url(raw: str | None) -> str | None:
    """`www.site.com/` -> `https://www.site.com`; drops fragments and tracking queries."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().strip("<>\"'")
    if not s or s.lower().startswith("mailto:"):
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        u = urlsplit(s)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname or "." not in u.hostname:
        return None
    path = re.sub(r"/{2,}", "/", u.path or "")
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    query = "&".join(p for p in u.query.split("&")
                     if p and not p.lower().startswith(("utm_", "ref=", "fbclid", "gclid")))
    return urlunsplit(("https", u.hostname.lower() + (f":{u.port}" if u.port else ""),
                       path, query, ""))


def url_key(raw: str | None) -> str | None:
    """Equality key: host without www + path, no scheme, no query, no trailing slash."""
    n = normalize_url(raw)
    if not n:
        return None
    u = urlsplit(n)
    host = (u.hostname or "").removeprefix("www.")
    return host + (u.path if u.path != "/" else "")


def host_of(raw: str | None) -> str:
    n = normalize_url(raw)
    return (urlsplit(n).hostname or "").removeprefix("www.") if n else ""


def registrable(host: str) -> str:
    """Last two labels (good enough for same-site checks; no public-suffix list)."""
    parts = host.lower().removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def same_site(a: str, b: str) -> bool:
    return bool(host_of(a)) and registrable(host_of(a)) == registrable(host_of(b))


def linkedin_profile(raw: str | None) -> str | None:
    """Canonical `https://www.linkedin.com/in/<handle>` or None."""
    n = normalize_url(raw)
    if not n:
        return None
    u = urlsplit(n)
    if not _LINKEDIN_IN.match(u.hostname or ""):
        return None
    m = re.match(r"^/in/([^/]+)", u.path)
    if not m:
        return None
    return f"https://www.linkedin.com/in/{m.group(1)}"


def github_profile(raw: str | None) -> str | None:
    """`https://github.com/<user>` for a user/org URL; None for repos and site pages."""
    n = normalize_url(raw)
    if not n:
        return None
    u = urlsplit(n)
    if (u.hostname or "").removeprefix("www.") != "github.com":
        return None
    m = _GITHUB_USER.match(u.path)
    if not m or m.group(1).lower() in _GITHUB_RESERVED:
        return None
    return f"https://github.com/{m.group(1)}"


def is_social(raw: str | None) -> bool:
    host = host_of(raw)
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def is_html_candidate(raw: str | None) -> bool:
    n = normalize_url(raw)
    return bool(n) and not NON_HTML.search(urlsplit(n).path)


def name_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) > 2
            and t not in {"the", "inc", "llc", "ltd", "limited", "corp", "company", "and"}}


def host_mentions(host: str, name: str) -> bool:
    """Does the host look like this company's own domain? e.g. payrail.io for 'PayRail'."""
    h = registrable(host).split(".")[0]
    tokens = name_tokens(name)
    return bool(tokens) and (h in tokens or "".join(sorted(tokens, key=len, reverse=True)[:2]) == h
                             or any(t in h for t in tokens if len(t) >= 4))
