import yaml
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    allow_methods: List[str] = Field(default_factory=lambda: ["*"])
    allow_headers: List[str] = Field(default_factory=lambda: ["*"])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Database
    db: DatabaseConfig
    database_url_override: Optional[str] = Field(default=None, validation_alias="DATABASE_URL")

    # CORS
    cors: Optional[CORSConfig] = None

    # COS (Tencent Cloud Object Storage)
    cos: Optional[COSConfig] = None

    # SMTP (Email)
    smtp: Optional[SMTPConfig] = None

    # Redis-backed verification codes and rate limits. If unset, the app uses
    # in-memory storage for local development.
    redis_url: Optional[str] = Field(default=None, validation_alias="REDIS_URL")
    verification_code_expire_seconds: int = 600
    verification_send_interval_seconds: int = 60

    # JWT
    jwt_secret_key: str = "your-secret-key-change-in-production"
    jwt_expire_minutes: int = 60 * 24  # 24 hours
    algorithm: str = "HS256"
    auth_cookie_name: str = "evenly_access_token"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return self.db.url


def load_settings() -> Settings:
    config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    if config_path.exists():
        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)
        return Settings(**(config_data or {}))
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
