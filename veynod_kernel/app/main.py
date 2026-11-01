from fastapi import FastAPI

from app.api import api_router
from app.engine.kernel_service_impl import PostgresKernelEngineService
from app.mcp_server.syscalls import get_kernel_engine, router as syscalls_router
from app.runtime import install_api_plane_guard, lifespan
from app.services.request_timing import install_request_timing

app = FastAPI(title="Veynod Kernel v4.0", version="4.0.0", lifespan=lifespan)
install_api_plane_guard(app)
app.include_router(syscalls_router)
app.include_router(api_router)
app.dependency_overrides[get_kernel_engine] = PostgresKernelEngineService.from_session_dep
install_request_timing(app)
