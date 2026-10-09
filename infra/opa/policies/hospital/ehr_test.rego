# Unit tests for ehr.rego. Run with: make opa-test
package hospital.ehr_test

import data.hospital.ehr

agent(name, roles, scopes) := {"user": "u", "roles": roles, "client": name, "scopes": scopes}

psych_note := {"id": "n1", "type": "DocumentReference", "labels": ["PSY"]}

plain_note := {"id": "n2", "type": "DocumentReference", "labels": []}

test_summary_agent_reads_notes if {
	ehr.tool_decision.allow with input as {"tool": "get_clinical_notes",
		"subject": agent("summary-agent", ["physician"], ["patient:read"])}
}

test_scheduling_agent_cannot_read_notes if {
	d := ehr.tool_decision with input as {"tool": "get_clinical_notes",
		"subject": agent("scheduling-agent", ["physician"], ["patient:read"])}
	not d.allow
	contains(d.reason, "scheduling-agent is not allowed to use get_clinical_notes")
}

test_request_signature_needs_its_scope if {
	d := ehr.tool_decision with input as {"tool": "request_signature",
		"subject": agent("prescription-agent", ["physician"], ["patient:read"])}
	not d.allow
	contains(d.reason, "token lacks a scope")
}

test_unknown_tool_is_denied if {
	d := ehr.tool_decision with input as {"tool": "delete_patient",
		"subject": agent("chart-agent", ["physician"], ["patient:read"])}
	not d.allow
	contains(d.reason, "unknown tool")
}

test_visible_tools_follow_agent_and_scope if {
	ehr.visible_tools == ["get_discharge_plan", "get_portal_messages", "search_patients"] with input as {
		"subject": agent("scheduling-agent", ["physician"], ["patient:read"])}
	ehr.visible_tools == ["get_allergies", "get_medications", "get_patient_summary"] with input as {
		"subject": agent("prescription-agent", ["physician"], ["patient:read"])}
}

test_no_scopes_means_no_tools if {
	ehr.visible_tools == [] with input as {"subject": agent("chart-agent", ["admin"], [])}
}

test_hospital_app_reads_the_chart_but_never_writes if {
	app := agent("hospital-ui", ["nurse"], ["patient:read"])
	ehr.tool_decision.allow with input as {"tool": "get_clinical_notes", "subject": app}
	not ehr.tool_decision.allow with input as {"tool": "request_signature", "subject": app}
}

test_nurse_does_not_see_psych_note if {
	w := ehr.withheld with input as {"subject": agent("chart-agent", ["nurse"], []),
		"resources": [psych_note, plain_note], "break_glass": false}
	count(w) == 1
	w[0].id == "n1"
	contains(w[0].reason, "PSY-labelled")
}

test_physician_sees_everything if {
	ehr.withheld == [] with input as {"subject": agent("chart-agent", ["physician"], []),
		"resources": [psych_note, plain_note], "break_glass": false}
}

test_break_the_glass_unlocks_for_nurse if {
	ehr.withheld == [] with input as {"subject": agent("chart-agent", ["nurse"], []),
		"resources": [psych_note], "break_glass": true}
}

test_coordinator_records_the_discharge if {
	ehr.tool_decision.allow with input as {"tool": "record_discharge",
		"subject": agent("orchestrator", ["physician"], ["discharge:order"])}
}

test_coordinator_reads_nothing if {
	d := ehr.tool_decision with input as {"tool": "get_clinical_notes",
		"subject": agent("orchestrator", ["physician"], ["patient:read", "discharge:order"])}
	not d.allow
	ehr.visible_tools == ["decide_patient_request", "record_discharge"] with input as {
		"subject": agent("orchestrator", ["physician"], ["patient:read", "discharge:order"])}
}

test_no_agent_records_a_discharge_without_the_order if {
	d := ehr.tool_decision with input as {"tool": "record_discharge",
		"subject": agent("transitions-agent", ["physician"], ["discharge:order"])}
	not d.allow
}

benefits_for(patient, assigned) := {"tool": "get_coverage", "patient_id": patient,
	"subject": agent("benefits-agent", ["physician"], ["patient:read"]),
	"assignment": {"bound": true, "patients": assigned}}

test_agent_reads_the_patient_of_its_task if {
	ehr.tool_decision.allow with input as benefits_for("jane-doe", ["jane-doe"])
}

test_agent_cannot_read_another_patient if {
	d := ehr.tool_decision with input as benefits_for("pt-87c46349", ["jane-doe"])
	not d.allow
	contains(d.reason, "benefits-agent is not assigned to patient pt-87c46349")
	contains(d.reason, "its task is about: jane-doe")
}

test_agent_without_a_task_reads_nobody if {
	d := ehr.tool_decision with input as benefits_for("jane-doe", [])
	not d.allow
	contains(d.reason, "has no task for any patient")
}

test_people_are_not_task_bound if {
	ehr.tool_decision.allow with input as {"tool": "get_medications", "patient_id": "pt-87c46349",
		"subject": agent("hospital-ui", ["physician"], ["patient:read"]),
		"assignment": {"bound": false, "patients": []}}
}

patient_subject := {"user": "jane.doe", "roles": ["patient"], "client": "hospital-ui",
	"scopes": ["portal:self"], "linked_patient": "jane-doe"}

test_patient_reads_own_medicines_and_requests if {
	ehr.tool_decision.allow with input as {"tool": "get_medications", "patient_id": "jane-doe",
		"subject": patient_subject}
	ehr.tool_decision.allow with input as {"tool": "update_consent", "patient_id": "jane-doe",
		"subject": patient_subject}
}

test_patient_cannot_open_someone_elses_record if {
	d := ehr.tool_decision with input as {"tool": "get_medications", "patient_id": "pt-87c46349",
		"subject": patient_subject}
	not d.allow
	contains(d.reason, "jane.doe may only access their own record")
}

test_patient_cannot_read_the_clinical_chart_or_decide if {
	not ehr.tool_decision.allow with input as {"tool": "get_clinical_notes",
		"patient_id": "jane-doe", "subject": patient_subject}
	not ehr.tool_decision.allow with input as {"tool": "decide_patient_request",
		"patient_id": "jane-doe", "subject": patient_subject}
}

test_portal_agent_files_requests_but_cannot_sign if {
	portal := {"user": "jane.doe", "roles": ["patient"], "client": "portal-agent",
		"scopes": ["portal:self"], "linked_patient": "jane-doe"}
	ehr.tool_decision.allow with input as {"tool": "submit_patient_request",
		"patient_id": "jane-doe", "subject": portal}
	not ehr.tool_decision.allow with input as {"tool": "request_signature",
		"patient_id": "jane-doe", "subject": portal}
}

test_patient_sees_own_sensitive_records if {
	count(ehr.withheld) == 0 with input as {"subject": patient_subject,
		"resources": [psych_note], "break_glass": false, "own_record": true}
	count(ehr.withheld) == 1 with input as {"subject": patient_subject,
		"resources": [psych_note], "break_glass": false, "own_record": false}
}

test_no_patient_argument_is_not_someone_elses_record if {
	# get_my_record has no patient_id: null must not count as "another patient"
	ehr.tool_decision.allow with input as {"tool": "get_my_record", "patient_id": null,
		"subject": patient_subject}
}
