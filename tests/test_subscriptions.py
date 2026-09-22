"""Double opt-in flow against an in-memory fake of the Xano tables."""
import itertools

import pytest

from weather import subscriptions, xano
from weather.config import Settings


@pytest.fixture
def fake(monkeypatch):
    tables = {xano.SUB_REQ_TABLE: [], xano.SUBSCRIBERS_TABLE: []}
    ids = itertools.count(1)
    sent = []

    def insert(s, t, row):
        row = {**row, "id": next(ids)}
        tables[t].append(row)
        return row

    def patch(s, t, rid, fields):
        next(r for r in tables[t] if r["id"] == rid).update(fields)

    monkeypatch.setattr(xano, "available", lambda s: True)
    monkeypatch.setattr(xano, "list_rows", lambda s, t: [dict(r) for r in tables[t]])
    monkeypatch.setattr(xano, "insert_row", insert)
    monkeypatch.setattr(xano, "patch_row", patch)
    monkeypatch.setattr(subscriptions.emailer, "send",
                        lambda s, to, subj, html, text, key, **kw: sent.append((to, subj, text)) or (True, "id"))
    return tables, sent, insert


S = Settings(resend_api_key="k", xano_meta_url="x", xano_token="t")


def test_signup_confirm_unsubscribe(fake):
    tables, sent, insert = fake
    insert(S, xano.SUB_REQ_TABLE, {"action": "subscribe", "email": "Pat@Example.com ", "token": "", "honeypot": "", "processed": ""})
    subscriptions.process(S)
    sub = tables[xano.SUBSCRIBERS_TABLE][0]
    assert sub["email"] == "pat@example.com" and sub["status"] == "pending"
    assert len(sent) == 1 and sub["token"] in sent[0][2]  # confirmation link carries the token
    assert subscriptions.active_subscribers(S) == []  # nobody gets reports before confirming

    insert(S, xano.SUB_REQ_TABLE, {"action": "subscribe", "email": "pat@example.com", "token": "", "honeypot": "", "processed": ""})
    subscriptions.process(S)
    assert len(sent) == 1  # no second confirmation within 24h

    insert(S, xano.SUB_REQ_TABLE, {"action": "confirm", "email": "", "token": sub["token"], "honeypot": "", "processed": ""})
    subscriptions.process(S)
    assert [r["email"] for r in subscriptions.active_subscribers(S)] == ["pat@example.com"]

    insert(S, xano.SUB_REQ_TABLE, {"action": "unsubscribe", "email": "", "token": sub["token"], "honeypot": "", "processed": ""})
    subscriptions.process(S)
    assert subscriptions.active_subscribers(S) == []
    assert all(r["processed"] for r in tables[xano.SUB_REQ_TABLE])


def test_bots_bad_emails_and_bad_tokens_are_ignored(fake):
    tables, sent, insert = fake
    for req in ({"action": "subscribe", "email": "bot@example.com", "honeypot": "http://spam"},
                {"action": "subscribe", "email": "not-an-email", "honeypot": ""},
                {"action": "confirm", "token": "guess", "honeypot": ""}):
        insert(S, xano.SUB_REQ_TABLE, {"token": "", "email": "", "processed": "", **req})
    res = subscriptions.process(S)
    assert res["ignored"] == 3 and not sent and not tables[xano.SUBSCRIBERS_TABLE]
