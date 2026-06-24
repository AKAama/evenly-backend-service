import logging

from fastapi import FastAPI
from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import engine
from app.routers import auth, ledgers, expenses, settlements, users

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Evenly - Multi-person Expense Splitting App",
    description="Backend API for collaborative expense tracking and settlement",
    version="1.0.0"
)


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.exception(
        "Database error while handling %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Database is not ready"},
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


@app.get("/")
async def root():
    return {"message": "Welcome to Evenly API"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/ready")
async def readiness_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except SQLAlchemyError as exc:
        logger.exception("Database readiness check failed", exc_info=exc)
        raise HTTPException(status_code=503, detail="Database is not ready") from exc

    return {"status": "ready"}


if __name__ == "__main__":
    import uvicorn
    import asyncio

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
