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
# Types whose normalised value is a canonical lookup key (folded name, URL key, lower-cased
# email, bare phone digits) rather than a typed value: stored and indexed for matching, not shown.
CANONICAL: frozenset[str] = frozenset({"keyword", "url", "contact"})
NORMALISABLE: frozenset[str] = frozenset({"number", "boolean", "date", "enum", "enum_list"}) | CANONICAL

# Filter operators by type. Text operators always run on the text as entered (`field.v`); eq/ne/in
# and the range operators run on the normalised value of a normalisable property.
TEXT_OPS = ("eq", "ne", "contains", "starts_with", "regex", "in", "exists")
RANGE_OPS = ("gt", "gte", "lt", "lte")


def filter_ops(ptype: str) -> tuple[str, ...]:
    return TEXT_OPS + (RANGE_OPS if ptype in ("number", "date") else ())


MAX_ENUM_VALUE_CHARS = 40          # a canonical enum value is a short label

# The keys that describe who a person is, for an agent's judgement (the repair agent's evidence,
# the normaliser's context); long texts are shown by their opening.
DESCRIBES_PERSON = ("linkedin", "email", "github", "website", "company", "role",
                    "linkedin_headline", "one_liner", "bio", "intro_bio", "about",
                    "experience", "sectors", "company_description", "notes", "entity_type",
                    "location")
DESCRIBE_CLIP = {"about": 600, "experience": 400, "notes": 800, "bio": 800,
                 "company_description": 400}
AUDIT_KEYS = frozenset({"enrichment_passage"})    # records of what was filed: never a fact to re-file
IDENTITY_KEYS = ("email", "phone_number", "linkedin", "github")


@dataclass(frozen=True)
class Spec:
    type: PropertyType
    values: tuple[str, ...] = ()      # seed canonical values (enum, enum_list)
    label: str | None = None          # overrides fields.label_for(key)
    description: str | None = None    # one sentence on what values it holds (agents read it)
    embed: bool | None = None         # overrides the type's default (text/list are embedded)


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
    "phone_number": Spec("contact"),
    "linkedin": Spec("url", label="LinkedIn"),
    "github": Spec("url", label="GitHub"),
    "instagram": Spec("url"),
    "website": Spec("url", description="The person's own site (portfolio, blog, personal page)"),
    "company_website": Spec("url", description="The current employer's website"),
    "related_links": Spec("url"),
    # who they are
    "role": Spec("keyword", embed=True,
                 description="Current job title, as the person, an upload or a data provider "
                             "states it"),
    # what the enrich assistant files from provider profiles through the storage agent
    "about": Spec("text", description="The person's own profile summary (LinkedIn about text) in "
                                      "their own words; providers drop its line breaks, only "
                                      "the lost spacing is restored"),
    "experience": Spec("text", description="Work history: title at company (years), one position "
                                           "per clause, most recent first"),
    "education": Spec("text", description="Education: degree, institution (years), one school "
                                          "per clause"),
    "skills": Spec("list", description="Skills listed on their profiles (payments, python, "
                                       "go-to-market…)"),
    "languages": Spec("list", description="Languages they speak"),
    "interests": Spec("list", description="Interests listed on their profiles (topics, causes, "
                                          "hobbies)"),
    "enrichment_passage": Spec("text", label="Enrichment passage",
                               description="The whole provider profile as one passage: about, "
                                           "title, work history, education, skills, languages, "
                                           "then the site and company notes, sectioned by "
                                           "source; compare a profile-wide description with it"),
    "seniority": Spec("enum", SENIORITY),
    # short values most people have, compared by meaning too (a title, a company, a place)
    "company": Spec("keyword", embed=True, description="Current employer or own company"),
    "industry": Spec("keyword", embed=True),
    "entity_type": Spec("keyword", embed=True,
                        description="What the record's organisation is (startup, fund, "
                                    "corporate…) as the upload stated it"),
    "location": Spec("keyword", embed=True),
    "city": Spec("keyword", embed=True),
    "region": Spec("enum", REGIONS),
    "linkedin_headline": Spec("text", label="LinkedIn headline"),
    "bio": Spec("text", description="A few sentences about the person: background, what they "
                                    "do now, notable facts"),
    "intro_bio": Spec("text", description="The introduction the community posted about them"),
    "one_liner": Spec("text", description="One sentence: who they are and what they build"),
    "github_summary": Spec("text", label="GitHub summary"),
    "sectors": Spec("list", description="Industry tags for their work (fintech, payments, ai…)"),
    "icp": Spec("list", label="ICP (ideal customer profile)",
                description="Who their company sells to (saas, consumer, enterprises…)"),
    "provides": Spec("list", description="What they can offer other members (introductions, "
                                         "expertise, services)"),
    "communities": Spec("enum_list", COMMUNITIES,
                        description="The community groups they belong to"),
    "needs_stated": Spec("enum_list", NEEDS,
                         description="What they said they are looking for (capital, hiring…)"),
    "needs_inferred": Spec("enum_list", NEEDS,
                           description="What we infer they need from their situation"),
    "investor": Spec("boolean", description="Whether they invest in startups"),
    # the company
    "company_description": Spec("text", description="What their company does, for whom"),
    "stage": Spec("enum", STAGES, description="The company's funding or maturity stage"),
    "model": Spec("enum", ("B2B", "B2C", "Marketplace"), description="Business model"),
    "arr": Spec("enum", ARR, label="ARR (annual recurring revenue)",
                description="Annual recurring revenue bucket"),
    "mrr": Spec("number", label="MRR (monthly recurring revenue)",
                description="Monthly recurring revenue in USD; ranges count as the lower bound"),
    "revenue_note": Spec("text", description="Free-text remarks on revenue or traction"),
    "funding": Spec("enum", FUNDING, description="Total funding raised, as a bucket"),
    "raised_funds": Spec("number", label="Raised funds (total funding raised)",
                         description="Total funding raised in USD"),
    # the record itself
    "record_kind": Spec("enum", ("Person", "Company", "Unidentifiable"),
                        description="What the record is: a person, a company or organisation, or "
                                    "a record whose identity cannot be resolved; agents may add "
                                    "kinds"),
    # the relationship
    "status": Spec("enum", ("Active", "Removed")),
    "membership": Spec("enum", ("Standard", "Premium")),
    "directory_tier": Spec("enum", ("Core", "Plus", "Removed"),
                           description="Their tier in the member directory"),
    "warmth": Spec("enum", ("Known", "Opted in", "Warm", "Hot"),
                   description="How warm the relationship with the community is"),
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
    "last_message": Spec("text", description="The last message exchanged with them"),
    "notes": Spec("text", description="Internal remarks: meetings, follow-ups, anything that "
                                      "fits no other property"),
    "col_source": Spec("keyword", label="Source"),
}
