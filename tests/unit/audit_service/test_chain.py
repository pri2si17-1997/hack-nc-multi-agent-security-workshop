from hacknc.audit_service.chain import GENESIS_HASH, compute_hash, verify


def make_chain(n: int) -> list[dict]:
    entries, prev = [], GENESIS_HASH
    for seq in range(1, n + 1):
        entry = {"seq": seq, "action": f"a{seq}", "decision": "allow", "details": {"k": seq},
                 "agent_chain": ["x"], "prev_hash": prev}
        entry["hash"] = compute_hash(entry)
        entries.append(entry)
        prev = entry["hash"]
    return entries


def test_intact_chain_verifies() -> None:
    result = verify(make_chain(5))
    assert result.ok and result.checked == 5
    assert result.head_hash == make_chain(5)[-1]["hash"]


def test_empty_chain_is_intact() -> None:
    assert verify([]).ok


def test_edited_entry_is_pinpointed() -> None:
    chain = make_chain(5)
    chain[2]["decision"] = "deny"  # entry #3
    result = verify(chain)
    assert not result.ok
    assert result.broken_at == 3
    assert "modified" in result.problem
    assert result.checked == 2


def test_deleted_entry_is_detected() -> None:
    chain = make_chain(5)
    del chain[1]  # entry #2
    result = verify(chain)
    assert result.broken_at == 2
    assert "missing" in result.problem


def test_rehashed_edit_still_breaks_the_next_link() -> None:
    """A smarter attacker also recomputes the edited entry's hash: the next entry catches it."""
    chain = make_chain(4)
    chain[1]["decision"] = "deny"
    chain[1]["hash"] = compute_hash(chain[1])
    result = verify(chain)
    assert result.broken_at == 3
    assert "does not link" in result.problem


def test_hash_ignores_key_order_but_not_content() -> None:
    a = {"seq": 1, "action": "x", "details": {"a": 1, "b": 2}}
    b = {"details": {"b": 2, "a": 1}, "action": "x", "seq": 1}
    assert compute_hash(a) == compute_hash(b)
    assert compute_hash(a) != compute_hash({**a, "action": "y"})
