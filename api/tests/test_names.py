"""The name rules every level shares (app/names.py)."""

from app import names


def test_full_name_rules():
    assert names.is_full_name("Ada Okafor") and names.is_full_name("Rwaida Gharib-Kane")
    assert names.is_full_name("O'Neil Mcdonald")
    for not_full in ("Ada", "Polly I.", "Ada O", "Jordan | Cofounder | UXUY", "Lena@Cohere Commerce",
                     "Raj - LootMogul accelerated by a16z", "Anna (she/her)", "DJ_Deep", "", None):
        assert not names.is_full_name(not_full), not_full
    assert names.tokens("  Ada   Okafor ") == ["ada", "okafor"] and names.tokens(None) == []


def test_title_case_for_names_and_for_provider_text():
    assert names.title_case("elizabeth hsu") == "Elizabeth Hsu"
    assert names.title_case("o'neil mcdonald-smith") == "O'Neil Mcdonald-Smith"
    assert names.title_case("maria de la cruz") == "Maria de La Cruz"
    assert names.title_case("Already Fine") == "Already Fine" and names.title_case("") == ""
    # providers that lower-case everything: small words stay, two-letter tokens are acronyms
    assert names.title_case("it service desk & incident manager at td", acronyms=True) == \
        "IT Service Desk & Incident Manager at TD"
    assert names.title_case("head of it", acronyms=True) == "Head of IT"
    assert names.title_case("go-to-market lead", acronyms=True) == "Go-To-Market Lead"
    assert names.title_case("supra its", acronyms=True) == "Supra Its"
    assert names.title_case("McKinsey & Company", acronyms=True) == "McKinsey & Company"
    assert names.title_case("english") == "English" and names.title_case("Yoruba") == "Yoruba"


def test_mojibake_and_broken_names():
    assert names.demojibake("AndrÃ© NiÃ±o") == "André Niño"
    assert names.demojibake("plain") == "plain"
    assert names.looks_broken("polly.israni@gmail.com") and names.looks_broken("www.site.com")
    assert names.looks_broken("agent 007") and not names.looks_broken("elizabeth hsu")
    assert names.COMPANY_WORDS.search("Sequoia Scout Fund III") and not names.COMPANY_WORDS.search("Ada Okafor")
