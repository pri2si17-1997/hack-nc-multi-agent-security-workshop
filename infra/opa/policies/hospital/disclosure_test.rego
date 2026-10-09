# Unit tests for disclosure.rego. Run with: make opa-test
package hospital.disclosure_test

import data.hospital.disclosure

benefits := {"user": "dr.morgana", "roles": ["physician"], "client": "benefits-agent",
	"scopes": ["disclose:prior-auth"]}

psych := {"id": "psych-note", "type": "DocumentReference", "labels": ["PSY"]}

spo2 := {"id": "spo2", "type": "Observation", "labels": [], "cited": true}

# A condition the order does not cite (e.g. Jane's prediabetes, attached "to help").
prediabetes := {"id": "prediabetes", "type": "Condition", "labels": [], "cited": false}

# The physician's order in the discharge plan: home oxygen, backed by the walking SpO2.
oxygen_order := {"evidence": ["spo2"], "diagnosis_codes": ["R09.02"]}

prior_auth(fields, resources) := {"purpose": "prior_authorization", "destination": "payer",
	"subject": benefits, "fields": fields, "resources": resources, "consent": {},
	"partner_enabled": true, "order": oxygen_order, "patient_id": "jane-doe",
	"assignment": {"bound": true, "patients": ["jane-doe"]}}

test_minimum_necessary_prior_auth if {
	d := disclosure.decision with input as prior_auth(["name", "dob", "service", "evidence"], [spo2])
	d.allow
	d.allowed_fields == ["dob", "evidence", "name", "service"]
	d.stripped_fields == []
	d.stripped_resources == []
}

test_oversharing_is_stripped_not_sent if {
	d := disclosure.decision with input as prior_auth(["name", "evidence", "clinical_notes"], [spo2, psych])
	d.allow
	d.stripped_fields[0].field == "clinical_notes"
	d.stripped_resources[0].id == "psych-note"
	contains(d.stripped_resources[0].reason, "not consented")
}

test_records_the_order_does_not_cite_are_stripped if {
	d := disclosure.decision with input as prior_auth(["name", "evidence"], [spo2, prediabetes])
	d.allow
	[stripped] := d.stripped_resources
	stripped.id == "prediabetes"
	contains(stripped.reason, "not cited by the physician's order")
}

test_a_sensitive_uncited_record_is_withheld_once if {
	d := disclosure.decision with input as prior_auth(["evidence"], [psych])
	[stripped] := d.stripped_resources
	contains(stripped.reason, "not consented")
}

test_prior_auth_without_an_order_is_denied if {
	d := disclosure.decision with input as object.remove(prior_auth(["name"], []), ["order"])
	not d.allow
	contains(d.reason, "no order in the discharge plan")
}

test_wrong_destination_is_denied if {
	d := disclosure.decision with input as object.union(prior_auth(["name"], []), {"destination": "pcp-clinic"})
	not d.allow
	contains(d.reason, "may only go to payer")
}

test_wrong_agent_is_denied if {
	rogue := object.union(benefits, {"client": "scheduling-agent"})
	d := disclosure.decision with input as object.union(prior_auth(["name"], []), {"subject": rogue})
	not d.allow
	contains(d.reason, "scheduling-agent is not allowed")
}

test_switched_off_partner_is_denied if {
	d := disclosure.decision with input as object.union(prior_auth(["name"], []), {"partner_enabled": false})
	not d.allow
	contains(d.reason, "switched off")
}

test_nurse_cannot_disclose if {
	nurse := object.union(benefits, {"roles": ["nurse"]})
	not disclosure.decision.allow with input as object.union(prior_auth(["name"], []), {"subject": nurse})
}

test_consent_lets_sensitive_go_to_pcp_only if {
	transitions := {"user": "dr.morgana", "roles": ["physician"], "client": "transitions-agent",
		"scopes": ["disclose:care-transition"]}
	base := {"purpose": "care_transition", "destination": "pcp-clinic", "subject": transitions,
		"fields": ["summary"], "resources": [psych], "partner_enabled": true}
	count(disclosure.decision.stripped_resources) == 1 with input as object.union(base, {"consent": {}})
	disclosure.decision.stripped_resources == [] with input as object.union(base,
		{"consent": {"shareSensitiveWithPcp": true}})
}

test_unknown_purpose if {
	not disclosure.decision.allow with input as {"purpose": "marketing", "subject": benefits}
}

scheduling := {"user": "dr.morgana", "roles": ["physician"], "client": "scheduling-agent",
	"scopes": ["disclose:scheduling"]}

test_follow_up_may_name_the_specialty_but_not_diagnoses if {
	d := disclosure.decision with input as {"purpose": "appointment", "destination": "pcp-clinic",
		"subject": scheduling, "fields": ["name", "dob", "reason", "within_days", "specialty", "diagnoses"],
		"resources": [], "consent": {}, "partner_enabled": true}
	d.allow
	d.allowed_fields == ["dob", "name", "reason", "specialty", "within_days"]
	d.stripped_fields[0].field == "diagnoses"
}

test_agent_cannot_disclose_another_patients_data if {
	d := disclosure.decision with input as object.union(prior_auth(["name"], []),
		{"patient_id": "pt-87c46349"})
	not d.allow
	contains(d.reason, "benefits-agent is not assigned to patient pt-87c46349")
}

test_agent_without_a_task_discloses_nothing if {
	d := disclosure.decision with input as object.union(prior_auth(["name"], []),
		{"assignment": {"bound": true, "patients": []}})
	not d.allow
	contains(d.reason, "has no task for any patient")
}
