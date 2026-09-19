"""Modular FastAPI APIRouters package for Bulgarian Invoice OCR Microservice."""

from .accounting import router as accounting_router
from .contractors import router as contractors_router
from .documents import router as documents_router
from .health import router as health_router
from .hitl import router as hitl_router
from .ingestion import router as ingestion_router
from .jobs import router as jobs_router
from .webhooks import router as webhooks_router

__all__ = [
    "accounting_router",
    "contractors_router",
    "documents_router",
    "health_router",
    "hitl_router",
    "ingestion_router",
    "jobs_router",
    "webhooks_router",
]
