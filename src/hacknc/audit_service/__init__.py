"""Audit service: an append-only, hash-chained log of every security-relevant event.

Each entry stores the hash of the entry before it, so editing or deleting any past entry
breaks every link after it, and ``/verify`` pinpoints where.
"""
