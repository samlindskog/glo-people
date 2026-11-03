"""Retiring a property whose meaning another one carries: `people.fold_property` replays its
entries into the other property's history under the usual trust rules and drops the field; the
CLI command retires the catalog entry too. And `people.rewrite_values`, the mechanical repair of
shown texts (here the sentence spacing a provider dropped)."""

import pytest

from app import cli, embedder, fields, people, properties
from app.config import get_settings
from app.history import Source
from app.mongo import db
from tests.conftest import import_people


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


async def _titles(ids: dict[str, str], key: str, titles: dict[str, str], kind: str = "agent") -> None:
    await properties.ensure([key], "import")
    for name, title in titles.items():
        tx = await people.Transaction.open(ids[name])
        assert tx is not None
        await tx.set_values({key: title}, Source(kind, "enrich/storage", "run:1/item:1"))
        await tx.commit(None)


async def test_fold_property_replays_entries_under_the_trust_rules(writer):
    await import_people(writer, "Name,Position\nAda Okafor,Founder\nBo Chen,\nCy Diaz,CTO\n", "p.csv", "member")
    ids = {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}
    # an agent's title for Ada (outranks the upload's role), one for Bo who has no role, the same
    # title Cy's upload gave
    await _titles(ids, "old_title", {"Ada Okafor": "CEO", "Bo Chen": "Engineer", "Cy Diaz": "CTO"})
    rep = await people.fold_property("old_title", "role", dry_run=True)
    assert rep["documents"] == 3 and rep["filled"] == 1 and rep["merged"] == 2
    assert rep["shown_changed"] == 2 and {s["name"] for s in rep["samples"]} == {"Ada Okafor", "Bo Chen"}
    assert (await db().people.find_one({"name.v": "Ada Okafor"})).get("old_title")   # dry run: nothing written

    rep = await people.fold_property("old_title", "role", dry_run=False)
    assert rep["documents"] == 3
    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert "old_title" not in ada and ada["role"]["v"] == "CEO" and ada["role"]["norm"]["v"] == "ceo"
    assert [e["v"] for e in ada["role"]["h"]] == ["Founder", "CEO"]                # both, in time order
    assert [e["src"]["kind"] for e in ada["role"]["h"]] == ["import", "agent"]
    bo = await db().people.find_one({"name.v": "Bo Chen"})
    assert "old_title" not in bo and bo["role"]["v"] == "Engineer"
    cy = await db().people.find_one({"name.v": "Cy Diaz"})
    assert "old_title" not in cy and cy["role"]["v"] == "CTO" and len(cy["role"]["h"]) == 1   # once
    assert cy["role"]["norm"]["v"] == "cto"                                           # the key stayed
    assert (await people.fold_property("old_title", "role", dry_run=False))["documents"] == 0
    # a human's role stays shown over a folded agent title; the title is in the history
    await people.update(None, ids["Bo Chen"], {"role": "Head of Engineering"}, source=Source("ui", "wendy"))
    await _titles(ids, "old_title", {"Bo Chen": "Engineer II"})
    await people.fold_property("old_title", "role", dry_run=False)
    bo = await db().people.find_one({"name.v": "Bo Chen"})
    assert bo["role"]["v"] == "Head of Engineering" and bo["role"]["h"][-1]["v"] == "Engineer II"
    with pytest.raises(people.PersonError):
        await people.fold_property("role", "role")
    # the same upload wrote both keys at once: the role the record showed stays shown
    await import_people(writer, "Name,Position,Old Title\nDi Evans,Co-Founder,Angel Investor\n", "m.csv", "member")
    await people.fold_property("old_title", "role", dry_run=False)
    di = await db().people.find_one({"name.v": "Di Evans"})
    assert di["role"]["v"] == "Co-Founder" and [e["v"] for e in di["role"]["h"]] == ["Angel Investor", "Co-Founder"]


async def test_fold_property_cli_retires_the_catalog_entry(writer, capsys):
    await import_people(writer, "Name,Emails\nAda Okafor,ada@x.io\n", "p.csv", "member")
    ids = {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}
    await _titles(ids, "old_emails", {"Ada Okafor": "ada@work.io"}, kind="import")
    assert await properties.is_known("old_emails")
    await cli.fold_property("old_emails", "email", yes=False)
    assert "(dry run)" in capsys.readouterr().out and await properties.is_known("old_emails")
    await cli.fold_property("old_emails", "email", yes=True)
    out = capsys.readouterr().out
    assert "1 folded" in out and "registry entry deleted" in out
    assert not await properties.is_known("old_emails")
    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert "old_emails" not in ada and ada["email"]["v"] == "ada@work.io"     # newest within a class
    assert ada["email"]["norm"]["v"] == "ada@work.io" and [e["v"] for e in ada["email"]["h"]] == ["ada@x.io", "ada@work.io"]
    await cli.fold_property("old_emails", "email", yes=True)                  # nothing left: fine
    assert "0 folded" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        await cli.fold_property("email", "phone_number", yes=True)           # a spec property stays


async def test_rewrite_values_repairs_shown_texts_as_derived_entries(writer, capsys):
    await import_people(writer, "Name,About\nAda Okafor,Builds payments.Previously Stripe.\nBo Chen,Fine text.\n",
                        "p.csv", "member")
    rep = await people.rewrite_values("about", fields.respace, dry_run=True)
    assert rep["documents"] == 2 and rep["changed"] == 1 and rep["samples"][0]["name"] == "Ada Okafor"
    await cli.repair_text("about", yes=True)
    assert "1 repaired" in capsys.readouterr().out
    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert ada["about"]["v"] == "Builds payments. Previously Stripe."
    assert ada["about"]["h"][0]["v"] == "Builds payments.Previously Stripe."    # the original kept
    assert ada["about"]["h"][-1]["derived"] is True and ada["about"]["h"][-1]["src"]["kind"] == "system"
    assert (await people.rewrite_values("about", fields.respace, dry_run=False))["changed"] == 0
    # a person's own later edit outranks the repair
    await people.update(None, str(ada["_id"]), {"about": "Builds payments.Previously Stripe."},
                        source=Source("ui", "wendy"))
    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert ada["about"]["v"] == "Builds payments.Previously Stripe."
