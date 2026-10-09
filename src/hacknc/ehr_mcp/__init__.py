"""ehr-mcp: the MCP gateway in front of the hospital EHR. The only way agents reach patient data.

Every request: Keycloak token validated (audience = ehr-mcp) -> OPA decides whether this agent,
acting for this human, may call this tool -> records filtered by sensitivity label -> audited.
"""
