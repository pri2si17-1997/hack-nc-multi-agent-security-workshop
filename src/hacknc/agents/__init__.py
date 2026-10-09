"""Specialist agents, each its own MCP server with its own Keycloak identity.

    medrec-agent        reconcile_medications       model proposes, rules verify
    benefits-agent      arrange_equipment           coverage + prior authorization (payer)
    scheduling-agent    book_follow_up              the follow-up visit (clinic)
    prescription-agent  prepare_prescriptions,      drafts for the physician's signature;
                        send_prescription           sends signed ones to the pharmacy
    summary-agent       draft_discharge_summary     for the physician's signature
    transitions-agent   send_summary_to_pcp         sends the signed summary (rules only)
    instructions-agent  write_patient_instructions  posts them to the patient portal
    chart-agent         answer_question             chat answers from the chart
    portal-agent        triage_message              files a patient's portal message

Each agent receives a token addressed to itself (from the coordinator), exchanges it for one
addressed to the gateway it needs, and only then touches data. ``servers.py`` has the agents,
``compose.py`` the rules, ``prompts.py`` the model instructions, ``toolkit.py`` what every
model-driven agent gets (``record_decision`` and the tool-call hooks).
"""
