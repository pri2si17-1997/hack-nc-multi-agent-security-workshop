package system.authz_test

import data.system.authz

test_services_can_evaluate_decisions if {
	authz.allow with input as {"method": "POST", "path": ["v1", "data", "hospital", "ehr", "tool_decision"]}
}

test_anyone_can_read_the_policies if {
	authz.allow with input as {"method": "GET", "path": ["v1", "policies"]}
}

test_nobody_can_replace_a_policy if {
	not authz.allow with input as {"method": "PUT", "path": ["v1", "policies", "hospital", "ehr"]}
}

test_nobody_can_write_or_delete_data if {
	not authz.allow with input as {"method": "PATCH", "path": ["v1", "data", "hospital"]}
	not authz.allow with input as {"method": "DELETE", "path": ["v1", "policies", "x"]}
}
