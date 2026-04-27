from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import engine, Base
from app.config import settings
from app.routers import auth, ledgers, expenses, settlements, users

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Evenly - Multi-person Expense Splitting App",
    description="Backend API for collaborative expense tracking and settlement",
    version="1.0.0"
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


if __name__ == "__main__":
    import uvicorn
    import asyncio

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
