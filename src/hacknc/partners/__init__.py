"""Mock outside organizations: the payer, the patient's pharmacy, her family doctor's clinic and
the state controlled-substance registry. One service plays all four.

Each one is a separate trust domain: it only accepts tokens whose audience is *its own* name,
and it keeps a log of exactly what it received, so the demo can show what left the hospital.
"""
