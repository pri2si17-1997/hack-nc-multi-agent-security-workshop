# Access to the hospital EHR through the MCP gateway (ehr-mcp).
#
# The gateway asks three questions:
#   tool_decision   may this agent, acting for this human, call this tool?   {allow, reason}
#   visible_tools   which tools should tools/list show this caller?          [names]
#   withheld        which of these records must be hidden from this caller?  [{id, reason}]
#
# input.subject = {user, roles, client, scopes}: `client` is the agent that called the gateway
# (the token's azp), `roles` are the human's, `scopes` are what this token may do.
package hospital.ehr

# Each tool, and the token scopes that unlock it (any one is enough).
tool_scopes := {
	"search_patients": {"patient:read"},
	"get_patient_summary": {"patient:read"},
	"get_medications": {"patient:read", "portal:self"},
	"get_allergies": {"patient:read"},
	"get_observations": {"patient:read"},
	"get_clinical_notes": {"patient:read"},
	"get_portal_messages": {"patient:read"},
	"get_coverage": {"patient:read"},
	"get_discharge_plan": {"patient:read"},
	# patient portal: portal:self is only granted to patients, for their own record
	"get_my_record": {"portal:self"},
	"get_patient_requests": {"patient:read", "portal:self"},
	"post_portal_message": {"portal:self"},
	"submit_patient_request": {"portal:self"},
	"update_consent": {"portal:self"},
	"decide_patient_request": {"discharge:order"},
	"request_signature": {"sign:request"},
	"post_patient_instructions": {"portal:write"},
	"record_discharge": {"discharge:order"},
}

chart_tools := {"get_patient_summary", "get_medications", "get_allergies", "get_observations",
	"get_clinical_notes", "get_portal_messages"}

# Minimum necessary: each agent gets only the tools its job needs.
agent_tools := {
	# the hospital app itself (a human looking at the chart, and the patient picker)
	"hospital-ui": {"search_patients", "get_my_record", "get_patient_requests",
		"update_consent"} | chart_tools,
	"chart-agent": {"search_patients", "get_patient_summary", "get_medications", "get_allergies",
		"get_observations", "get_clinical_notes"},
	"medrec-agent": {"get_patient_summary", "get_medications", "get_allergies", "get_observations"},
	"summary-agent": {"get_patient_summary", "get_medications", "get_observations",
		"get_clinical_notes", "request_signature"},
	"prescription-agent": {"get_patient_summary", "get_medications", "get_allergies",
		"request_signature"},
	"benefits-agent": {"get_patient_summary", "get_observations", "get_coverage"},
	# the follow-up plan and the patient's own messages (scheduling preferences): no problems,
	# vitals or notes
	"scheduling-agent": {"search_patients", "get_discharge_plan", "get_portal_messages"},
	"instructions-agent": {"get_patient_summary", "get_medications", "get_portal_messages",
		"post_patient_instructions"},
	# the discharge coordinator reads nothing; it only records the discharge the physician ordered
	"orchestrator": {"record_discharge", "decide_patient_request"},
	# acts for the patient: records the message, files the request; decides nothing
	"portal-agent": {"get_medications", "post_portal_message", "submit_patient_request"},
}

sensitive_labels := {"PSY", "HIV"}

# ------------------------------------------------------------------ tools

default tool_decision := {"allow": false, "reason": "no rule matched"}

tool_decision := {"allow": true, "reason": "allowed"} if count(tool_denials) == 0

tool_decision := {"allow": false, "reason": concat("; ", sort([r | some r in tool_denials]))} if {
	count(tool_denials) > 0
}

tool_denials contains sprintf("unknown tool '%s'", [input.tool]) if {
	not tool_scopes[input.tool]
}

tool_denials contains sprintf("%s is not allowed to use %s", [input.subject.client, input.tool]) if {
	tool_scopes[input.tool]
	not input.tool in object.get(agent_tools, input.subject.client, set())
}

tool_denials contains sprintf("token lacks a scope for %s (needs one of: %s)", [input.tool, concat(", ", sort(tool_scopes[input.tool]))]) if {
	tool_scopes[input.tool]
	count(tool_scopes[input.tool] & {s | some s in input.subject.scopes}) == 0
}

# Task binding: an agent may only touch the patient(s) its current task is about. The
# discharge coordinator tells the gateway which (input.assignment, from POST /tasks/assign);
# people using the hospital app are not bound (assignment.bound is false).
tool_denials contains sprintf("%s is not assigned to patient %s (its task is about: %s)", [input.subject.client, input.patient_id, concat(", ", input.assignment.patients)]) if {
	input.assignment.bound
	is_string(input.patient_id)
	count(input.assignment.patients) > 0
	not input.patient_id in {p | some p in input.assignment.patients}
}

tool_denials contains sprintf("%s has no task for any patient right now", [input.subject.client]) if {
	input.assignment.bound
	is_string(input.patient_id)
	count(input.assignment.patients) == 0
}

# Patients: their own record only (the EHR links the portal account to one record).
tool_denials contains sprintf("%s may only access their own record", [input.subject.user]) if {
	"patient" in input.subject.roles
	is_string(input.patient_id)
	input.patient_id != object.get(input.subject, "linked_patient", null)
}

# tools/list: show exactly the tools this caller could call (no denial reasons).
visible_tools := sort([tool |
	some tool, needed in tool_scopes
	tool in object.get(agent_tools, input.subject.client, set())
	count(needed & {s | some s in input.subject.scopes}) > 0
])

# ------------------------------------------------------------------ records

is_physician if "physician" in input.subject.roles

# Sensitive records need a physician, or an active break-the-glass grant for this patient.
withheld := [{"id": r.id, "type": r.type, "reason": reason} |
	some r in input.resources
	some label in r.labels
	label in sensitive_labels
	not is_physician
	not input.break_glass
	not input.own_record   # a patient may see their own sensitive records
	reason := sprintf("%s-labelled record: physician role or break-the-glass required", [label])
]
