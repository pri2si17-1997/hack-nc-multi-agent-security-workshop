# Workshop overlay

Hand-written records layered on top of the curated Synthea patients when the EHR server loads.
Synthea has no clinical notes, no controlled-substance refills for our demo patient, and no
sensitivity labels, so the security story needs these. Every record is tagged
`workshop-overlay` so it is clear what is not Synthea.

| Field | Meaning |
|---|---|
| `meta.security` | HL7 confidentiality labels (`PSY` psychiatry, `HIV`). OPA withholds labelled records from non-physicians. |
| `controlledSchedule` | DEA schedule (simplified; real FHIR would use an extension or the medication's RxNorm data). `C-II` refills need physician approval. |
| `coverage` | Replaces the coverage derived from Synthea claims. |

The *Prompt injection* scenario's attack message is not stored here: with the scenario on, the
EHR gateway adds it to the portal messages of whichever patient is open
(`staged_injection` in `src/hacknc/ehr_mcp/server.py`).
