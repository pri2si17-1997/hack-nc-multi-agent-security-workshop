# Unit tests for approval.rego. Run with: make opa-test
package hospital.approval_test

import data.hospital.approval

physician_in_app := {"user": "dr.morgana", "roles": ["physician"], "client": "hospital-ui",
	"scopes": ["rx:approve", "rx:draft"]}

nurse_in_app := {"user": "nurse.gawain", "roles": ["nurse"], "client": "hospital-ui",
	"scopes": ["rx:draft"]}

gateway_for_physician := {"user": "dr.morgana", "roles": ["physician"], "client": "ehr-mcp",
	"scopes": ["approval:request"]}

sender_for_physician := {"user": "dr.morgana", "roles": ["physician"], "client": "disclosure-mcp",
	"scopes": ["approval:request"]}

pending := {"status": "pending", "expired": false}

approved := {"status": "approved", "expired": false}

# A consume call as the disclosure gateway makes it: Jane's prescription, sent as Jane's.
signed_for_jane := {"action": "consume", "patient_id": "jane-doe", "kind": "prescription",
	"submitted": {"patient_id": "jane-doe", "kind": "prescription"}}

test_gateway_can_file_request if {
	approval.decision.allow with input as {"action": "create", "subject": gateway_for_physician}
}

test_agent_cannot_file_request_directly if {
	d := approval.decision with input as {"action": "create", "subject": object.union(
		gateway_for_physician, {"client": "pharmacy-agent"},
	)}
	not d.allow
	contains(d.reason, "only the EHR gateway")
}

test_physician_can_approve if {
	approval.decision.allow with input as {"action": "decide", "subject": physician_in_app,
		"request": pending}
}

test_nurse_cannot_approve if {
	d := approval.decision with input as {"action": "decide", "subject": nurse_in_app,
		"request": pending}
	not d.allow
	contains(d.reason, "only physicians")
	contains(d.reason, "rx:approve")
}

test_agent_cannot_approve_even_for_a_physician if {
	agent := object.union(physician_in_app, {"client": "pharmacy-agent"})
	d := approval.decision with input as {"action": "decide", "subject": agent, "request": pending}
	not d.allow
	contains(d.reason, "not an agent")
}

test_cannot_decide_twice if {
	d := approval.decision with input as {"action": "decide", "subject": physician_in_app,
		"request": approved}
	not d.allow
	contains(d.reason, "not pending")
}

test_expired_request_cannot_be_approved if {
	not approval.decision.allow with input as {"action": "decide", "subject": physician_in_app,
		"request": {"status": "pending", "expired": true}}
}

test_gateway_consumes_matching_approval if {
	approval.decision.allow with input as object.union(signed_for_jane, {"subject": sender_for_physician,
		"request": approved, "hash_matches": true})
}

test_tampered_prescription_is_denied if {
	d := approval.decision with input as object.union(signed_for_jane, {"subject": sender_for_physician,
		"request": approved, "hash_matches": false})
	not d.allow
	contains(d.reason, "differs from what the physician signed")
}

test_missing_hash_check_fails_closed if {
	not approval.decision.allow with input as object.union(signed_for_jane, {
		"subject": sender_for_physician, "request": approved})
}

test_pending_request_cannot_be_consumed if {
	not approval.decision.allow with input as object.union(signed_for_jane, {
		"subject": sender_for_physician, "request": pending, "hash_matches": true})
}

test_break_glass_allowed_with_justification if {
	approval.decision.allow with input as {"action": "break_glass", "subject": nurse_in_app,
		"break_glass": {"enabled": true, "justification": "Unconscious patient in ER, checking meds"}}
}

test_break_glass_needs_real_justification if {
	d := approval.decision with input as {"action": "break_glass", "subject": nurse_in_app,
		"break_glass": {"enabled": true, "justification": "because"}}
	not d.allow
	contains(d.reason, "justification")
}

test_break_glass_off_by_default if {
	d := approval.decision with input as {"action": "break_glass", "subject": nurse_in_app,
		"break_glass": {"enabled": false, "justification": "Unconscious patient in ER, checking meds"}}
	not d.allow
	contains(d.reason, "disabled")
}

test_unknown_action_is_denied if {
	not approval.decision.allow with input as {"action": "delete_everything",
		"subject": physician_in_app}
}

test_ehr_gateway_cannot_act_on_signed_content if {
	d := approval.decision with input as object.union(signed_for_jane, {"subject": gateway_for_physician,
		"request": approved, "hash_matches": true})
	not d.allow
	contains(d.reason, "only the disclosure gateway")
}

test_signature_cannot_be_used_for_another_patient if {
	d := approval.decision with input as object.union(signed_for_jane, {
		"subject": sender_for_physician, "request": approved, "hash_matches": true,
		"submitted": {"patient_id": "pt-87c46349", "kind": "prescription"}})
	not d.allow
	contains(d.reason, "another patient")
}

test_signed_prescription_cannot_be_sent_as_a_summary if {
	d := approval.decision with input as object.union(signed_for_jane, {
		"subject": sender_for_physician, "request": approved, "hash_matches": true,
		"submitted": {"patient_id": "jane-doe", "kind": "discharge_summary"}})
	not d.allow
	contains(d.reason, "for a prescription, not a discharge_summary")
}

test_missing_patient_binding_fails_closed if {
	not approval.decision.allow with input as {"action": "consume", "patient_id": "jane-doe",
		"kind": "prescription", "subject": sender_for_physician, "request": approved,
		"hash_matches": true}
}
