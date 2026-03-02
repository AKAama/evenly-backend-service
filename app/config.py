import os
import yaml
from pathlib import Path
from typing import Optional, List
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


class COSConfig(BaseModel):
    secret_id: str
    secret_key: str
    region: str
    bucket: str
    cdn_domain: Optional[str] = None

    @property
    def base_url(self) -> str:
        if self.cdn_domain:
            return f"https://{self.cdn_domain}"
        return f"https://{self.bucket}.cos.{self.region}.myqcloud.com"


class SMTPConfig(BaseModel):
    secret_id: str
    secret_key: str
    from_email: str
    from_name: str = "Evenly"
    template_id: Optional[str] = None


class CORSConfig(BaseModel):
    allow_origins: List[str]
    allow_credentials: bool = True
    allow_methods: List[str] = ["*"]
    allow_headers: List[str] = ["*"]


class Settings(BaseModel):
    # Database
    db: DatabaseConfig

    # CORS
    cors: Optional[CORSConfig] = None

    # COS (Tencent Cloud Object Storage)
    cos: Optional[COSConfig] = None

    # SMTP (Email)
    smtp: Optional[SMTPConfig] = None

    # JWT
    jwt_secret_key: str = "your-secret-key-change-in-production"
    jwt_expire_minutes: int = 60 * 24  # 24 hours
    algorithm: str = "HS256"

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
