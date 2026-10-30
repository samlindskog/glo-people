"""Property specs: the typed properties, declared in source code.

A property without a spec is free text and embeddable. A spec gives a property a type and, for
the enum types, the canonical values its text is normalised onto. `python -m app.cli classify`
suggests specs from the data; a developer pastes them here and deploys. The live enum sets (these
seeds plus anything added later) are kept by `app.properties`.
"""

from dataclasses import dataclass
from typing import Literal

PropertyType = Literal["text", "keyword", "url", "contact", "number", "boolean", "date",
                       "enum", "enum_list", "list"]

# text: prose, compared by meaning. keyword: short identifying strings (names, titles, places),
# matched literally. list: free-form items (tags). The other types hold one kind of value each.
EMBEDDABLE: frozenset[str] = frozenset({"text", "list"})
NORMALISABLE: frozenset[str] = frozenset({"number", "boolean", "date", "enum", "enum_list"})


@dataclass(frozen=True)
class Spec:
    type: PropertyType
    values: tuple[str, ...] = ()      # seed canonical values (enum, enum_list)
    label: str | None = None          # overrides fields.label_for(key)


TEXT = Spec("text")

NEEDS = ("Capital", "Partners", "Clients", "Growth", "Hiring", "Talent", "Engineering",
         "Payments", "FinOps", "Legal", "Network", "Dealflow", "Exit")
COMMUNITIES = ("AI & B2B SaaS", "Fintech & Crypto", "Health & Bio", "Investors",
               "Consumer Apps & Social", "Growth & GTM", "Deeptech & Hardware",
               "Gaming, Media & Creators", "D2C, CPG & Commerce", "Future of Work & Talent",
               "Community Builders", "Emerging Markets")
REGIONS = ("SF Bay Area", "Los Angeles", "New York", "Boston", "Chicago", "Seattle",
           "Austin/Texas", "Miami/Florida", "DC Metro", "US (other)", "Canada", "UK", "Germany",
           "France", "Netherlands", "Brazil", "Colombia", "India", "UAE", "Taiwan", "Hong Kong",
           "Australia")
STAGES = ("Ideation", "Pre-seed", "Seed", "Pre-series A", "Series A", "Series B+",
          "Early revenue", "Scale-up", "SMB", "Does not apply")
ARR = ("Pre-revenue", "< $999K", "$1M - $5M", "$5M - $10M", "$10M - $25M", "$25M - $100M",
       "$100M - $1B", "$1B+")
FUNDING = ("Bootstrapping", "< $100K", "$100K - $500K", "$500K - $1M", "$1M - $5M", "$5M - $10M",
           "$10M - $25M", "$25M - $100M", "$100M - $500M", "$500M - $1B", "$1B+")
SENIORITY = ("Student/Intern", "IC", "Senior/Lead", "VP/Head/Director", "Founding",
             "Founder/C-level")

SPECS: dict[str, Spec] = {
    # identity and contact
    "name": Spec("keyword"),
    "first_name": Spec("keyword"),
    "last_name": Spec("keyword"),
    "email": Spec("contact"),
    "emails": Spec("contact"),
    "phone_number": Spec("contact"),
    "phones": Spec("contact"),
    "linkedin": Spec("url", label="LinkedIn"),
    "github": Spec("url", label="GitHub"),
    "instagram": Spec("url"),
    "website": Spec("url"),
    "related_links": Spec("url"),
    # who they are
    "role": Spec("keyword"),
    "title_enriched": Spec("keyword", label="Title (enriched)"),
    "seniority": Spec("enum", SENIORITY),
    "company": Spec("keyword"),
    "industry": Spec("keyword"),
    "entity_type": Spec("keyword"),
    "location": Spec("keyword"),
    "city": Spec("keyword"),
    "region": Spec("enum", REGIONS),
    "linkedin_headline": Spec("text", label="LinkedIn headline"),
    "bio": TEXT,
    "intro_bio": TEXT,
    "one_liner": TEXT,
    "github_summary": Spec("text", label="GitHub summary"),
    "sectors": Spec("list"),
    "icp": Spec("list", label="ICP"),
    "provides": Spec("list"),
    "communities": Spec("enum_list", COMMUNITIES),
    "needs_stated": Spec("enum_list", NEEDS),
    "needs_inferred": Spec("enum_list", NEEDS),
    "investor": Spec("boolean"),
    # the company
    "company_description": TEXT,
    "stage": Spec("enum", STAGES),
    "model": Spec("enum", ("B2B", "B2C", "Marketplace")),
    "arr": Spec("enum", ARR, label="ARR"),
    "mrr": Spec("number", label="MRR"),
    "revenue_note": TEXT,
    "funding": Spec("enum", FUNDING),
    "raised_funds": Spec("number"),
    # the relationship
    "status": Spec("enum", ("Active", "Removed")),
    "membership": Spec("enum", ("Standard", "Premium")),
    "directory_tier": Spec("enum", ("Core", "Plus", "Removed")),
    "warmth": Spec("enum", ("Known", "Opted in", "Warm", "Hot")),
    "new_member": Spec("boolean"),
    "in_whatsapp": Spec("boolean", label="In WhatsApp"),
    "do_not_contact": Spec("boolean"),
    "pitcher": Spec("enum", ("Yes", "No", "Not applicable")),
    "sponsor": Spec("enum", ("No", "Yes", "Yes, this event", "Yes, future events",
                             "Not interested")),
    "joined": Spec("date"),
    "last_contact": Spec("date"),
    "linkedin_connected": Spec("date", label="LinkedIn connected"),
    "messages": Spec("number"),
    "last_message": TEXT,
    "notes": TEXT,
    "col_source": Spec("keyword", label="Source"),
}
