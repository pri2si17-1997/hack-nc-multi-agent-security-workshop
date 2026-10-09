from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from hacknc.common.auth import TokenClaims
from hacknc.ui import presenters


def test_personas() -> None:
    assert [p.username for p in presenters.PERSONAS] == ["dr.morgana", "nurse.gawain",
                                                         "admin.percival", "jane.doe"]
    assert [p.initials for p in presenters.PERSONAS][:3] == ["MP", "GO", "PG"]
    assert presenters.home_for("patient") == "/portal"


def test_primary_role_and_home() -> None:
    claims = TokenClaims({"realm_access": {"roles": ["offline_access", "nurse"]}})
    assert presenters.primary_role(claims) == "nurse"
    assert presenters.primary_role(TokenClaims({})) == "unknown"
    assert presenters.home_for("admin") == "/admin"
    assert presenters.home_for("physician") == "/ward"


def test_navigation_by_role() -> None:
    assert [n["path"] for n in presenters.nav_for("admin")] == ["/admin", "/admin/audit",
                                                                "/admin/incident"]
    assert [n["path"] for n in presenters.nav_for("nurse")] == ["/ward"]


def test_finale_flags_stage_only_the_wrong_patient() -> None:
    flags = presenters.finale_flags()
    assert flags["ai.model"] and not flags["break_glass_enabled"]
    assert [k for k, on in flags.items() if k.startswith("scenario.") and on] == \
        ["scenario.wrong_patient"]
    assert all(on for k, on in flags.items() if k.startswith(("agent.", "partner.")))


def test_flag_groups_cover_every_switch() -> None:
    groups = presenters.flag_groups({"agent.medrec-agent": False, "scenario.oversharing": True})
    keys = {i["key"]: i["on"] for g in groups for i in g["items"]}
    assert keys["agent.medrec-agent"] is False
    assert keys["scenario.oversharing"] is True
    assert keys["partner.payer"] is True
    assert "break_glass_enabled" in keys
    assert keys["ai.model"] is True
    assert [g["title"] for g in groups] == ["AI model", "Agents", "Outside organizations",
                                            "Scenarios"]


def test_labels_of() -> None:
    assert presenters.labels_of({"meta": {"security": [{"code": "PSY"}]}}) == ["PSY"]
    assert presenters.labels_of({}) == []


def test_grafana_trace_url_points_at_tempo() -> None:
    url = presenters.grafana_trace_url("http://localhost:3000/", "abc123")
    parsed = urlparse(url)
    panes = json.loads(parse_qs(parsed.query)["panes"][0])
    assert parsed.path == "/explore"
    assert panes["t"]["queries"][0]["query"] == "abc123"
    assert "panelsState" not in panes["t"]


def test_grafana_trace_url_can_select_a_span() -> None:
    url = presenters.grafana_trace_url("http://localhost:3000", "abc123", "def456")
    panes = json.loads(parse_qs(urlparse(url).query)["panes"][0])
    assert panes["t"]["panelsState"] == {"trace": {"spanId": "def456"}}


def test_disclosure_rows_get_the_partners_payload_newest_first() -> None:
    from hacknc.ui.audit_views import attach_payloads

    def row(seq, decision="allow", reason="sent"):
        return {"seq": seq, "partner": "payer", "decision": decision, "reason": reason}

    rows = [row(1), row(2), row(3, "deny", "DENIED by disclosure policy: unknown purpose")]
    received = [{"partner": "payer", "body": {"n": "only the newest is still logged"}}]
    attach_payloads(rows, received)
    assert "payload" not in rows[0] and rows[1]["payload"] == {"n": "only the newest is "
                                                                    "still logged"}
    assert "payload" not in rows[2]


def test_a_user_without_a_hospital_role_is_not_sent_in_circles() -> None:
    assert presenters.home_for("unknown") == "/?error=no-role"
    assert presenters.home_for("nurse") == "/ward"


def test_refusals_inside_exception_groups_stay_refusals() -> None:
    from hacknc.agents.runtime import ToolDenied
    from hacknc.ui.routes.backends import _find

    denied = ToolDenied("DENIED by policy: not yours")
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [denied])])
    assert _find(group, (ToolDenied,)) is denied
    assert _find(ExceptionGroup("x", [OSError("down")]), (ToolDenied,)) is None


def test_portal_suggestions_use_the_patients_own_medicines() -> None:
    from hacknc.ui.presenters import drug_word, portal_suggestions

    jane = [{"medication": {"text": "Oxycodone hydrochloride 5 MG Oral Tablet"}, "dose": "5 mg",
             "status": "active", "controlledSchedule": "C-II"},
            {"medication": {"text": "24 HR Metformin hydrochloride 500 MG Extended Release"},
             "status": "on-hold"}]
    assert portal_suggestions(jane) == {
        "refill": "Can I get a refill of my oxycodone? I only have two tablets left.",
        "raise": "It isn't working well enough. Please raise my oxycodone to 10 mg.",
        "side_effect": "Since yesterday the metformin makes me dizzy."}
    norris = [{"medication": {"text": "1 ML DOCEtaxel 20 MG/ML Injection"}, "status": "active"},
              {"medication": {"text": "Simvistatin 10 MG Oral Tablet"}, "status": "active"}]
    assert "simvistatin to 20 mg" in portal_suggestions(norris)["raise"]   # a tablet first
    assert drug_word("ferrous sulfate 325 MG Oral Tablet") == "ferrous sulfate"
    assert portal_suggestions([]) == {}
