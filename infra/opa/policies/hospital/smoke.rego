# Phase 1 placeholder policy: proves OPA is running and answering questions.
# The real hospital policies arrive in Phase 2.
#
# Try it:
#   curl -s localhost:8181/v1/data/hospital/smoke/allow \
#     -d '{"input": {"scopes": ["patient:read"]}}'
package hospital.smoke

default allow := false

# Allow when the caller's token carries the patient:read scope.
allow if "patient:read" in input.scopes
