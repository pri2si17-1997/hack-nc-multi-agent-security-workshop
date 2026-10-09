"""The rogue-admin script edits rows behind the chain's back; verify always notices."""

from __future__ import annotations

import pytest

from hacknc.audit_service import tamper
from hacknc.audit_service.store import AuditStore
from hacknc.common.audit import AuditEvent


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "audit.db"
    store = AuditStore(path)
    store.append(AuditEvent(action="mcp.tool_call", decision="allow",
                            details={"tool": "get_medications"}), "ehr-mcp")
    store.append(AuditEvent(action="mcp.tool_call", decision="deny", severity="warning",
                            reason="benefits-agent is not assigned to patient pt-2",
                            details={"tool": "get_coverage"}), "ehr-mcp")
    store.append(AuditEvent(action="security.alert", decision="deny"), "orchestrator")
    store.append(AuditEvent(action="policy.decision", decision="deny"), "opa")
    return path


def test_cover_up_rewrites_the_gateways_blocked_call(db_path, capsys) -> None:
    tamper.main(["--db", str(db_path), "--cover-up"])
    assert "entry #2" in capsys.readouterr().out
    store = AuditStore(db_path)
    entry = next(e for e in store.recent() if e["seq"] == 2)
    assert (entry["decision"], entry["severity"], entry["reason"]) == \
        ("allow", "info", "get_coverage: 1 returned")
    result = store.verify()
    assert not result.ok and result.broken_at == 2


def test_default_flips_the_latest_deny(db_path) -> None:
    tamper.main(["--db", str(db_path)])
    result = AuditStore(db_path).verify()
    assert not result.ok and result.broken_at == 4


def test_delete_leaves_a_gap(db_path) -> None:
    tamper.main(["--db", str(db_path), "--delete", "3"])
    result = AuditStore(db_path).verify()
    assert not result.ok and result.broken_at == 3 and "missing" in result.problem


def test_cover_up_needs_a_blocked_call(tmp_path) -> None:
    path = tmp_path / "empty.db"
    AuditStore(path)
    with pytest.raises(SystemExit, match="no blocked gateway call"):
        tamper.main(["--db", str(path), "--cover-up"])
