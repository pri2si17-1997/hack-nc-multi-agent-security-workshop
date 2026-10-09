from hacknc.audit_service.store import AuditStore
from hacknc.common.audit import AuditEvent


def test_append_links_entries_and_verifies() -> None:
    store = AuditStore(":memory:")
    first = store.append(AuditEvent(action="auth.login", decision="allow"), "hospital-ui")
    second = store.append(AuditEvent(action="token.exchange", decision="deny",
                                     agent_chain=["orchestrator", "billing-agent"]), "hospital-ui")
    assert (first["seq"], second["seq"]) == (1, 2)
    assert second["prev_hash"] == first["hash"]
    assert store.verify().ok
    assert store.count() == 2


def test_recent_is_newest_first_and_filters() -> None:
    store = AuditStore(":memory:")
    for decision in ("allow", "deny", "allow"):
        store.append(AuditEvent(action="x", decision=decision), "svc")
    assert [e["seq"] for e in store.recent()] == [3, 2, 1]
    assert [e["seq"] for e in store.recent(decision="deny")] == [2]
    assert store.recent(limit=1)[0]["seq"] == 3


def test_json_fields_round_trip() -> None:
    store = AuditStore(":memory:")
    store.append(AuditEvent(action="x", agent_chain=["a", "b"], details={"n": 1}), "svc")
    entry = store.recent()[0]
    assert entry["agent_chain"] == ["a", "b"]
    assert entry["details"] == {"n": 1}


def test_direct_sql_edit_is_caught_by_verify() -> None:
    store = AuditStore(":memory:")
    for _ in range(3):
        store.append(AuditEvent(action="x", decision="deny"), "svc")
    store.raw_connection().execute("UPDATE entries SET decision = 'allow' WHERE seq = 2")
    result = store.verify()
    assert not result.ok and result.broken_at == 2


def test_unknown_filters_are_ignored() -> None:
    store = AuditStore(":memory:")
    store.append(AuditEvent(action="x"), "svc")
    assert len(store.recent(**{"1=1; DROP TABLE entries; --": "x"})) == 1


def test_recent_reads_a_period_by_sequence_number() -> None:
    store = AuditStore(":memory:")
    for _ in range(6):
        store.append(AuditEvent(action="x"), "svc")
    assert [e["seq"] for e in store.recent(after=3)] == [6, 5, 4]
    assert [e["seq"] for e in store.recent(before=3)] == [2, 1]
    assert [e["seq"] for e in store.recent(after=1, before=5, limit=2)] == [4, 3]
    assert [e["seq"] for e in store.recent(after=0)] == [6, 5, 4, 3, 2, 1]
