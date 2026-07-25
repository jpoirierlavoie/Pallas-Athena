"""Portail client (spec L1) — second App Engine service « portail ».

This package is the CLIENT-facing portal (document intake). The lawyer-facing
service remains the repo's main app (a future rename may move it under a
``juriste/`` sibling — user decision 2026-07-25). Isolation is by
construction: ``client.wsgi:app`` registers ONLY the portal blueprint, so no
main-service route exists in that process even though the whole codebase
ships in the image (pinned by tests/test_portail_app.py).
"""
