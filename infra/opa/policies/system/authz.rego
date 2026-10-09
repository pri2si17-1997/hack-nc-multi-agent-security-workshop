# Who may use OPA's own REST API (OPA runs with --authorization=basic and asks this policy
# before serving every request).
#
# Read-only: services evaluate decisions (POST /v1/data/..., which never writes) and anyone may
# read the policies (GET). Nothing may change a policy or data document over HTTP (PUT, PATCH,
# DELETE): the rules come only from the files mounted read-only into the container. Otherwise
# a compromised agent could simply replace "deny" with "allow".
package system.authz

default allow := false

allow if input.method in {"GET", "POST"}
