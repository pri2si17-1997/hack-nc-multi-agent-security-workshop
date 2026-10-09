# Human-in-the-loop: the physician signature queue (prescriptions, discharge summaries).
#
# Who may file, sign, and act on signature requests, and who may break the glass.
# Each rule below adds a human-readable reason to `deny`; an action is allowed only when
# `deny` is empty. Services query `data.hospital.approval.decision`.
package hospital.approval

# Apps a human uses directly. Agents are never in this set, so an agent can never approve.
human_apps := {"hospital-ui"}

# Agents file drafts through the EHR gateway; signed content leaves the hospital only through
# the disclosure gateway.
filing_gateway := "ehr-mcp"

acting_gateway := "disclosure-mcp"

clinical_roles := {"physician", "nurse"}

known_actions := {"create", "decide", "consume", "break_glass"}

default decision := {"allow": false, "reason": "unknown action"}

decision := {"allow": true, "reason": "allowed"} if {
	input.action in known_actions
	count(deny) == 0
}

decision := {"allow": false, "reason": concat("; ", sort([r | some r in deny]))} if {
	input.action in known_actions
	count(deny) > 0
}

has_any_role(roles) if {
	some role in input.subject.roles
	role in roles
}

# ------------------------------------------------------------- create (agent files a request)

deny contains "only the EHR gateway may file signature requests" if {
	input.action == "create"
	input.subject.client != filing_gateway
}

deny contains "token lacks the approval:request scope" if {
	input.action == "create"
	not "approval:request" in input.subject.scopes
}

# ------------------------------------------------------------- decide (approve / reject)

deny contains "approvals must come from a human in the hospital app, not an agent" if {
	input.action == "decide"
	not input.subject.client in human_apps
}

deny contains "only physicians may sign prescriptions and discharge summaries" if {
	input.action == "decide"
	not "physician" in input.subject.roles
}

deny contains "token lacks the rx:approve scope" if {
	input.action == "decide"
	not "rx:approve" in input.subject.scopes
}

deny contains sprintf("request is %s, not pending", [input.request.status]) if {
	input.action == "decide"
	input.request.status != "pending"
}

deny contains "request has expired" if {
	input.action == "decide"
	input.request.expired
}

# ------------------------------------------------------------- consume (fulfil the approval)

deny contains "only the disclosure gateway may act on signed content" if {
	input.action == "consume"
	input.subject.client != acting_gateway
}

deny contains sprintf("request is %s, not approved", [input.request.status]) if {
	input.action == "consume"
	input.request.status != "approved"
}

deny contains "approval has expired" if {
	input.action == "consume"
	input.request.expired
}

# Missing hash_matches counts as a mismatch (fail closed).
deny contains "content differs from what the physician signed" if {
	input.action == "consume"
	not input.hash_matches == true
}

# A signature is for one patient and one kind of document: Jane's signed prescription can't
# be sent as someone else's, or as a discharge summary. Missing counts as a mismatch.
deny contains "the signature is for another patient" if {
	input.action == "consume"
	not object.get(input, ["submitted", "patient_id"], null) == input.patient_id
}

deny contains sprintf("the signature is for a %s, not a %s", [input.kind, object.get(input, ["submitted", "kind"], "document")]) if {
	input.action == "consume"
	not object.get(input, ["submitted", "kind"], null) == input.kind
}

# ------------------------------------------------------------- break the glass

deny contains "break-the-glass is disabled" if {
	input.action == "break_glass"
	not input.break_glass.enabled == true
}

deny contains "emergency access must be requested in the hospital app" if {
	input.action == "break_glass"
	not input.subject.client in human_apps
}

deny contains "only clinicians can break the glass" if {
	input.action == "break_glass"
	not has_any_role(clinical_roles)
}

deny contains "a justification of at least 15 characters is required" if {
	input.action == "break_glass"
	count(trim_space(object.get(input.break_glass, "justification", ""))) < 15
}
