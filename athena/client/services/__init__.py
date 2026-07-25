"""Portal-side service layer: read-only invitations, quarantine storage,
Cloud Tasks enqueue. Every Google client here is lazy — the portal boots
without any of the infrastructure existing (and tests import freely)."""
