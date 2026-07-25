"""Main-service orchestration helpers that compose several subsystems.

Distinct from ``models/`` (single-collection data access) and ``utils/``
(pure helpers): a service touches Firebase Auth + Firestore + outbound email
in one operation. Introduced by the portail client (spec L1 §6.2).
"""
