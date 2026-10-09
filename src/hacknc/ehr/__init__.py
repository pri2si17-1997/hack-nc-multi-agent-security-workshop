"""The hospital's electronic health record (EHR): synthetic patient data and how to query it.

``records`` knows nothing about users, tokens or policy. Access control lives in the MCP
gateway (``hacknc.ehr_mcp``), which is the only door to this data.
"""
