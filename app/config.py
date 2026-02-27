import yaml
from pathlib import Path
from pydantic import BaseModel


class DatabaseConfig(BaseModel):
    host: str
    port: int
    database: str
    user: str
    password: str

    @property
    def url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


class Settings(BaseModel):
    # Database
    db: DatabaseConfig

    # JWT
    secret_key: str = "your-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 24 hours

    @property
    def database_url(self) -> str:
        return self.db.url


def load_settings() -> Settings:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if config_path.exists():
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
        return Settings(**config_data)
    # Fallback to default
    return Settings(
        db=DatabaseConfig(
            host="localhost",
            port=5432,
            database="evenly",
            user="postgres",
            password="postgres"
        )
    )


settings = load_settings()
