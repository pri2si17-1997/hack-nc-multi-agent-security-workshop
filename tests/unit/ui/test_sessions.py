from __future__ import annotations

import time

from hacknc.common.auth import TokenClaims, TokenSet
from hacknc.ui.sessions import SessionStore

USER = TokenClaims({"preferred_username": "dr.morgana"})


def test_create_and_get(make_token) -> None:
    store = SessionStore()
    session = store.create(TokenSet(access_token=make_token()), USER)
    assert store.get(session.id) == session
    assert len(session.id) > 30  # unguessable


def test_unknown_or_missing_id_returns_none() -> None:
    store = SessionStore()
    assert store.get("nope") is None
    assert store.get(None) is None


def test_old_sessions_expire(make_token) -> None:
    store = SessionStore(max_age_seconds=-1)
    session = store.create(TokenSet(access_token=make_token()), USER)
    assert store.get(session.id) is None


def test_update_tokens_keeps_previous_refresh_and_id_token(make_token) -> None:
    store = SessionStore()
    session = store.create(TokenSet(access_token="old", refresh_token="r1", id_token="i1"), USER)
    updated = store.update_tokens(session.id, TokenSet(access_token=make_token()))
    assert updated.tokens.refresh_token == "r1"
    assert updated.tokens.id_token == "i1"
    assert store.get(session.id).tokens.access_token == updated.tokens.access_token


def test_delete(make_token) -> None:
    store = SessionStore()
    session = store.create(TokenSet(access_token=make_token()), USER)
    assert store.delete(session.id) == session
    assert store.get(session.id) is None


def test_access_expiry_check(make_token) -> None:
    store = SessionStore()
    session = store.create(TokenSet(access_token=make_token(exp=int(time.time()) + 30)), USER)
    assert session.access_expires_within(60)
    assert not session.access_expires_within(10)
