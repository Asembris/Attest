"""Attest's HTTP surface. See app.py — the human checkpoint does not soften here."""

from attest.api.app import app, get_service
from attest.api.service import AuditService, NotResumable, RunNotFound, ServiceError

__all__ = [
    "AuditService",
    "NotResumable",
    "RunNotFound",
    "ServiceError",
    "app",
    "get_service",
]
