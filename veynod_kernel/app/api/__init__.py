"""Aggregate API router (admin, runner, read-only DAG)."""

from fastapi import APIRouter

from app.api.admin_routes import router as admin_router
from app.api.cell_routes import router as cell_router
from app.api.code_intel_routes import router as code_intel_router
from app.api.dag_routes import router as dag_router
from app.api.runner_routes import router as runner_router

api_router = APIRouter()
api_router.include_router(admin_router)
api_router.include_router(cell_router)
api_router.include_router(code_intel_router)
api_router.include_router(runner_router)
api_router.include_router(dag_router)

__all__ = ["api_router"]
