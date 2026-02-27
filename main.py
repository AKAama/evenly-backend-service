from fastapi import FastAPI

from app.database import engine, Base
from app.routers import auth, ledgers, expenses, settlements, users

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Evenly - Multi-person Expense Splitting App",
    description="Backend API for collaborative expense tracking and settlement",
    version="1.0.0"
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
