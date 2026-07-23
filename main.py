import logging
from time import perf_counter

from fastapi import FastAPI
from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import engine
from app.routers import auth, ledgers, expenses, settlements, test_users, users, audit, platform_users, admin_ops
from app.services.access_log import format_access_line, try_user_hint_from_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Evenly - Multi-person Expense Splitting App",
    description="Backend API for collaborative expense tracking and settlement",
    version="1.0.0"
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """把客户端 IP / X-Client 绑到 contextvars，供审计写入使用。"""
    from app.services.request_context import bind_request_context, reset_request_context

    tokens = None
    try:
        tokens = bind_request_context(request)
    except Exception:
        logger.exception("绑定请求上下文失败 path=%s", request.url.path)
    try:
        return await call_next(request)
    finally:
        reset_request_context(tokens)


@app.middleware("http")
async def request_timing_middleware(request: Request, call_next):
    """每个 HTTP 请求打一行中文可读访问日志。"""
    from app.services.request_context import get_request_ip, get_request_source

    started_at = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        elapsed_ms = (perf_counter() - started_at) * 1000
        response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
        response.headers["Server-Timing"] = f'app;dur={elapsed_ms:.2f};desc="FastAPI"'
        return response
    finally:
        elapsed_ms = (perf_counter() - started_at) * 1000
        is_slow = elapsed_ms >= settings.slow_request_threshold_ms
        # 探活请求降噪：正常 200 不打日志
        path = request.url.path
        if path in {"/health", "/ready", "/"} and status_code < 400 and not is_slow:
            pass
        else:
            line = format_access_line(
                method=request.method,
                path=path,
                status_code=status_code,
                duration_ms=elapsed_ms,
                slow=is_slow,
                client_source=get_request_source(),
                client_ip=get_request_ip(),
                user_hint=try_user_hint_from_request(request),
            )
            logger.log(logging.WARNING if is_slow or status_code >= 500 else logging.INFO, "%s", line)


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError

    if isinstance(exc, (OperationalError, SATimeoutError)):
        logger.exception(
            "数据库连接异常（超时/断线）| %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        detail = "数据库连接异常，请稍后重试"
    else:
        logger.exception(
            "数据库异常 | %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        detail = "数据库暂时不可用"
    return JSONResponse(
        status_code=503,
        content={"detail": detail},
    )

# CORS middleware
if settings.cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allow_origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=settings.cors.allow_methods,
        allow_headers=settings.cors.allow_headers,
    )

# Include routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(ledgers.router)
app.include_router(expenses.router)
app.include_router(settlements.router)
app.include_router(test_users.router)
app.include_router(audit.router)
app.include_router(platform_users.router)
app.include_router(admin_ops.router)


@app.get("/")
async def root():
    return {"message": "Welcome to Evenly API"}


@app.get("/health")
async def health_check():
    from app.services.redis_client import redis_status

    redis = redis_status()
    return {
        "status": "healthy",
        "redis": redis,
    }


@app.get("/ready")
async def readiness_check():
    from app.services.redis_client import redis_status

    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except SQLAlchemyError as exc:
        logger.exception("就绪检查：数据库不可用", exc_info=exc)
        raise HTTPException(status_code=503, detail="Database is not ready") from exc

    redis = redis_status()
    # Redis is optional: app is ready without it, but surface status for ops.
    return {
        "status": "ready",
        "database": "ok",
        "redis": redis,
    }


if __name__ == "__main__":
    import uvicorn
    import asyncio

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
