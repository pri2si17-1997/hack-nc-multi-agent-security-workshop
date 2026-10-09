"""disclosure-mcp: the only way data leaves the hospital.

Agents never call outside organizations directly. They ask this gateway, which checks the
disclosure with OPA (who, to whom, for what purpose, which fields), strips what isn't allowed,
sends it with a token addressed only to that partner, and records it for the patient's
accounting of disclosures.
"""
