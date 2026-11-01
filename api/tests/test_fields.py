"""The text toolkit every level shares (app/fields.py)."""

from app import fields


def test_clean_decodes_entities_and_strips_invisible_spacers():
    assert fields.clean("Strategy &amp; ops ⠀⠀ leader\u200b") == "Strategy & ops leader"
    assert fields.clean("  two\n lines\t here ") == "two lines here"
    assert fields.clean(None) == "" and fields.clean(42) == "42"
    assert fields.clean("﻿bom nbsp") == "bom nbsp"
    assert fields.clean(" Met at demo day;  \n\n follow  up \n", keep_lines=True) == \
        "Met at demo day;\nfollow up"


def test_distinct_keeps_first_occurrence_by_key_and_drops_empties():
    assert fields.distinct(["Payments", " payments ", "Fintech", "", None, "fintech"]) == ["Payments", "Fintech"]
    assert fields.distinct(["b", "a", "b"], key=str) == ["b", "a"]
    assert fields.distinct([]) == []


def test_sentence_keeps_the_rest_of_the_line():
    assert fields.sentence("linkedIn profile matched; GitHub summarised") == "LinkedIn profile matched; GitHub summarised"
    assert fields.sentence("") == ""
