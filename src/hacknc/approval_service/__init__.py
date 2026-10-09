"""Approval service: the physician's signature queue (prescriptions, discharge summaries),
emergency break-the-glass grants, and the demo switches.

Agents may *request* a signature (through the EHR gateway) but can never sign. A physician
signs in the hospital app with their own token. Each signature is bound to a fingerprint of
the exact content, can be used once (by the disclosure gateway), and expires.
"""
