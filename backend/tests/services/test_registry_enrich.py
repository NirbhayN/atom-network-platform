# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Registry description enrichment — agent fills Message Item, humans stay sovereign.

The dangerous mutants:
  * LOCK TRAMPLE — the agent overwriting a human-edited description, or setting
    `updated_by` itself (which would freeze XSD re-ingest refreshes for the field).
  * SILENT WIPE — an LLM failure or junk xpath clearing cells that had text.
These tests pin: only empty, unlocked cells are written; locked/filled cells
survive; LLM failure is fail-open per message; provenance lands in
constraint_sources without claiming the human-edit lock.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.api_registry import ApiField, ApiMessage
import app.services.api_registry_enrich as enrich_mod
from app.services.api_registry_enrich import enrich_message_items


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[ApiMessage.__table__, ApiField.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def seed(db, api_name="ReqPing", fields=()):
    msg = ApiMessage(api_name=api_name, sample_xml=f"<{api_name}/>")
    db.add(msg)
    db.flush()
    rows = []
    for i, spec in enumerate(fields):
        f = ApiField(message_id=msg.id, position=i, xml_tag=spec["tag"],
                     xpath=spec["xpath"], message_item=spec.get("message_item"),
                     updated_by=spec.get("updated_by"))
        db.add(f)
        rows.append(f)
    db.commit()
    return msg, rows


def llm_returning(descriptions, calls=None):
    async def fake(system, user, *, schema, agent_name, tool_name="x", **kw):
        if calls is not None:
            calls.append(user)
        return {"descriptions": descriptions}
    return fake


def run(db, monkeypatch, fake_llm, **kw):
    monkeypatch.setattr("app.core.llm.call_llm_structured", fake_llm)
    return asyncio.run(enrich_message_items(db, **kw))


def test_fills_empty_cells_and_records_agent_provenance(db, monkeypatch):
    _, (f1, f2) = seed(db, fields=[
        {"tag": "ver", "xpath": "ReqPing/Head/@ver"},
        {"tag": "ts", "xpath": "ReqPing/Head/@ts"},
    ])
    counts = run(db, monkeypatch, llm_returning([
        {"xpath": "ReqPing/Head/@ver", "description": "Version of the API"},
        {"xpath": "ReqPing/Head/@ts", "description": "Time of request from the creator"},
    ]))
    assert counts["fields_described"] == 2
    assert counts["messages_processed"] == 1
    assert f1.message_item == "Version of the API"
    assert f2.message_item == "Time of request from the creator"
    # Provenance recorded; the human-edit lock is NOT claimed.
    assert f1.constraint_sources["agent"] == {"message_item": "description_enrichment"}
    assert f1.updated_by is None


def test_human_locked_field_is_never_touched(db, monkeypatch):
    _, (locked,) = seed(db, fields=[
        {"tag": "ver", "xpath": "ReqPing/Head/@ver",
         "message_item": "Human text", "updated_by": "po@npci"},
    ])
    counts = run(db, monkeypatch, llm_returning(
        [{"xpath": "ReqPing/Head/@ver", "description": "Agent text"}]))
    assert locked.message_item == "Human text"
    assert counts["fields_described"] == 0
    assert counts["human_locked_skipped"] == 1


def test_already_filled_cell_kept_unless_overwrite(db, monkeypatch):
    _, (f1,) = seed(db, fields=[
        {"tag": "ver", "xpath": "ReqPing/Head/@ver", "message_item": "Old agent text"},
    ])
    counts = run(db, monkeypatch, llm_returning(
        [{"xpath": "ReqPing/Head/@ver", "description": "New text"}]))
    assert f1.message_item == "Old agent text"
    assert counts["already_filled_skipped"] == 1

    counts = run(db, monkeypatch, llm_returning(
        [{"xpath": "ReqPing/Head/@ver", "description": "New text"}]), overwrite=True)
    assert f1.message_item == "New text"


def test_llm_failure_is_fail_open_per_message(db, monkeypatch):
    seed(db, api_name="ReqA", fields=[{"tag": "x", "xpath": "ReqA/x"}])
    _, (ok_field,) = seed(db, api_name="ReqB", fields=[{"tag": "y", "xpath": "ReqB/y"}])

    async def flaky(system, user, **kw):
        if "ReqA" in user:
            raise RuntimeError("boom")
        return {"descriptions": [{"xpath": "ReqB/y", "description": "Y thing"}]}

    counts = run(db, monkeypatch, flaky)
    assert counts["messages_failed"] == 1
    assert counts["messages_processed"] == 1
    assert ok_field.message_item == "Y thing"


def test_unknown_xpath_and_empty_description_are_ignored(db, monkeypatch):
    _, (f1,) = seed(db, fields=[{"tag": "ver", "xpath": "ReqPing/Head/@ver"}])
    counts = run(db, monkeypatch, llm_returning([
        {"xpath": "ReqPing/NoSuchPath", "description": "orphan"},
        {"xpath": "ReqPing/Head/@ver", "description": "   "},
    ]))
    assert f1.message_item is None
    assert counts["fields_described"] == 0


def test_api_names_filter_scopes_the_sweep(db, monkeypatch):
    _, (fa,) = seed(db, api_name="ReqA", fields=[{"tag": "x", "xpath": "ReqA/x"}])
    _, (fb,) = seed(db, api_name="ReqB", fields=[{"tag": "y", "xpath": "ReqB/y"}])
    calls = []
    run(db, monkeypatch, llm_returning(
        [{"xpath": "ReqA/x", "description": "X thing"}], calls), api_names=["ReqA"])
    assert len(calls) == 1 and "ReqA" in calls[0]
    assert fa.message_item == "X thing"
    assert fb.message_item is None


def test_overlong_description_is_capped(db, monkeypatch):
    _, (f1,) = seed(db, fields=[{"tag": "ver", "xpath": "ReqPing/Head/@ver"}])
    run(db, monkeypatch, llm_returning(
        [{"xpath": "ReqPing/Head/@ver", "description": "x" * 500}]))
    assert len(f1.message_item) == enrich_mod._MAX_DESCRIPTION_CHARS
