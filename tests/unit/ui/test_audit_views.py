from hacknc.ui.audit_views import attack_notes


def entry(seq, action, decision, chain, trace="t1", **details):
    return {"seq": seq, "action": action, "decision": decision, "agent_chain": chain,
            "trace_id": trace, "details": details}


MARKER = entry(1, "scenario.staged", "info", ["orchestrator", "scheduling-agent"],
               scenario="prompt_injection")


def test_a_staged_attack_labels_that_agents_refusals_in_the_same_trace() -> None:
    entries = [
        MARKER,
        entry(2, "agent.blocked", "deny", ["orchestrator", "scheduling-agent"]),
        entry(3, "disclosure.blocked", "deny", ["scheduling-agent", "disclosure-mcp", "pcp"]),
        entry(4, "agent.decision", "info", ["orchestrator", "scheduling-agent"]),  # ordinary
        entry(5, "mcp.tool_call", "deny", ["benefits-agent", "ehr-mcp"]),          # other agent
        entry(6, "agent.blocked", "deny", ["orchestrator", "scheduling-agent"], trace="t2"),
    ]
    attack_notes(entries, [MARKER])
    labelled = {e["seq"]: e["attack"]["label"] for e in entries if "attack" in e}
    assert labelled == {1: "Prompt injection", 2: "Prompt injection", 3: "Prompt injection"}
    assert "portal message" in entries[0]["attack"]["style"]


def test_withheld_data_counts_as_what_the_attack_caused() -> None:
    marker = entry(1, "scenario.staged", "info", ["orchestrator", "benefits-agent"],
                   scenario="oversharing")
    sent = entry(2, "disclosure.sent", "allow", ["benefits-agent", "disclosure-mcp", "payer"],
                 withheld=["psychiatry note"])
    attack_notes([sent], [marker])
    assert sent["attack"]["label"] == "Over-disclosure"


def test_no_markers_no_labels() -> None:
    entries = [entry(2, "agent.blocked", "deny", ["orchestrator", "scheduling-agent"])]
    assert "attack" not in attack_notes(entries, [])[0]
