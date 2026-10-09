# What may leave the hospital, to whom, for what purpose (HIPAA "minimum necessary").
#
# The disclosure gateway (disclosure-mcp) asks `decision` before every send:
#   input.purpose     what the disclosure is for (see `purposes`)
#   input.destination which outside organization
#   input.subject     the calling agent (client) and the human it acts for (user, roles, scopes)
#   input.fields      the fields the agent wants to send
#   input.resources   records attached as evidence, with their sensitivity labels and whether
#                     the physician's order cites them (``cited``)
#   input.order       for prior authorization: what the order in the discharge plan cites
#   input.consent     the patient's sharing preferences
#   input.partner_enabled  whether the admin has this partner switched on
#   input.patient_id  whose data it is
#   input.assignment  task binding, as at the EHR gateway: {bound, patients}
#
# The answer says whether to send at all, and exactly which fields and records may go.
package hospital.disclosure

purposes := {
	"eligibility": {"destination": "payer", "agent": "benefits-agent", "scope": "disclose:prior-auth",
		"fields": {"member_id", "name", "dob"}},
	"prior_authorization": {"destination": "payer", "agent": "benefits-agent", "scope": "disclose:prior-auth",
		"fields": {"member_id", "name", "dob", "service", "diagnosis_codes", "evidence"},
		"cited_only": true},
	"registry_query": {"destination": "nc-csrs", "agent": "prescription-agent", "scope": "disclose:registry",
		"fields": {"name", "dob", "drug"}},
	"prescription": {"destination": "retail-pharmacy", "agent": "prescription-agent",
		"scope": "disclose:prescription", "fields": {"name", "dob", "prescription", "signature"}},
	"appointment": {"destination": "pcp-clinic", "agent": "scheduling-agent", "scope": "disclose:scheduling",
		"fields": {"name", "dob", "reason", "within_days", "specialty"}},
	"care_transition": {"destination": "pcp-clinic", "agent": "transitions-agent",
		"scope": "disclose:care-transition", "fields": {"name", "dob", "document_type", "summary"}},
}

sensitive_labels := {"PSY", "HIV"}

rule := purposes[input.purpose]

default decision := {"allow": false, "reason": "unknown purpose", "allowed_fields": [],
	"stripped_fields": [], "stripped_resources": []}

decision := {
	"allow": count(denials) == 0,
	"reason": reason,
	"allowed_fields": sort([f | some f in input.fields; f in rule.fields]),
	"stripped_fields": [{"field": f, "reason": sprintf("not needed for %s (minimum necessary)", [input.purpose])} |
		some f in input.fields
		not f in rule.fields
	],
	"stripped_resources": array.concat(sensitive_stripped, uncited_stripped),
} if rule

sensitive_stripped := [{"id": r.id, "type": r.type, "reason": sprintf("%s-labelled record: patient has not consented to share it outside the hospital", [label])} |
	some r in input.resources
	some label in r.labels
	label in sensitive_labels
	not consented
]

# Minimum necessary for a request backed by a physician's order (prior authorization): only
# the records the order cites may go. An agent can't attach the rest of the chart "to help".
cited_only if object.get(rule, "cited_only", false) == true

sensitive_ids := {s.id | some s in sensitive_stripped}

uncited_stripped := [{"id": r.id, "type": r.type, "reason": sprintf("not cited by the physician's order (minimum necessary for %s)", [input.purpose])} |
	cited_only
	some r in input.resources
	not r.cited == true
	not r.id in sensitive_ids
]

reason := "allowed" if count(denials) == 0

reason := concat("; ", sort([d | some d in denials])) if count(denials) > 0

denials contains sprintf("%s data may only go to %s, not %s", [input.purpose, rule.destination, input.destination]) if {
	input.destination != rule.destination
}

denials contains sprintf("%s is not allowed to make %s disclosures", [input.subject.client, input.purpose]) if {
	input.subject.client != rule.agent
}

denials contains sprintf("token lacks %s", [rule.scope]) if {
	not rule.scope in input.subject.scopes
}

denials contains "disclosures are made on behalf of the treating physician" if {
	not "physician" in input.subject.roles
}

denials contains sprintf("no order in the discharge plan calls for %s", [input.purpose]) if {
	cited_only
	count(object.get(input, ["order", "evidence"], [])) == 0
	count(object.get(input, ["order", "diagnosis_codes"], [])) == 0
}

denials contains sprintf("%s is switched off by the admin", [input.destination]) if {
	input.partner_enabled == false
}

# Task binding, as at the EHR gateway: an agent may only disclose data about the patient(s)
# its current task is about (the coordinator binds each task before delegating it).
denials contains sprintf("%s is not assigned to patient %s (its task is about: %s)", [input.subject.client, input.patient_id, concat(", ", input.assignment.patients)]) if {
	input.assignment.bound
	count(input.assignment.patients) > 0
	not input.patient_id in {p | some p in input.assignment.patients}
}

denials contains sprintf("%s has no task for any patient right now", [input.subject.client]) if {
	input.assignment.bound
	count(input.assignment.patients) == 0
}

# Sensitive records may go to the family doctor only with the patient's consent; never elsewhere.
consented if {
	input.purpose == "care_transition"
	input.consent.shareSensitiveWithPcp == true
}
