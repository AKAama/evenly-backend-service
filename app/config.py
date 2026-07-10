import yaml
from pathlib import Path
from typing import Any, Optional, List
from pydantic import AliasChoices, BaseModel, Field
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
    from_name: str
    template_id: Optional[str] = None


class CORSConfig(BaseModel):
    allow_origins: List[str]
    allow_credentials: bool
    allow_methods: List[str]
    allow_headers: List[str]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
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
    verification_code_expire_seconds: int
    verification_send_interval_seconds: int

    # Opt-in, server-side token for local test-only endpoints.
    test_admin_token: Optional[str] = Field(default=None, validation_alias="TEST_ADMIN_TOKEN")

    # JWT
    jwt_secret_key: str
    jwt_expire_minutes: int
    algorithm: str
    auth_cookie_name: str
    auth_cookie_secure: bool
    auth_cookie_samesite: str
    apple_client_id: str

    # OpenAI-backed voice expense drafts. The key is server-side only.
    openai_api_key: Optional[str] = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "DASHSCOPE_API_KEY"))
    openai_transcription_model: str
    openai_text_model: str
    openai_url: str = Field(validation_alias=AliasChoices("OPENAI_URL", "DASHSCOPE_RESPONSES_URL"))

    # Tencent Cloud ASR (实时语音识别 WebSocket)
    asr_appid: Optional[str] = Field(default=None, validation_alias="ASR_APPID")
    asr_secret_id: Optional[str] = Field(default=None, validation_alias="ASR_SECRET_ID")
    asr_secret_key: Optional[str] = Field(default=None, validation_alias="ASR_SECRET_KEY")
    asr_engine_model_type: str = Field(default="16k_zh", validation_alias="ASR_ENGINE_MODEL_TYPE")
    asr_endpoint: str = Field(default="wss://asr.cloud.tencent.com/asr/v2/", validation_alias="ASR_ENDPOINT")
    asr_needvad: int = Field(default=1, validation_alias="ASR_NEEDVAD")
    asr_vad_silence_time: int = Field(default=800, validation_alias="ASR_VAD_SILENCE_TIME")
    asr_filter_modal: int = Field(default=2, validation_alias="ASR_FILTER_MODAL")
    asr_filter_punc: int = Field(default=0, validation_alias="ASR_FILTER_PUNC")
    asr_convert_num_mode: int = Field(default=1, validation_alias="ASR_CONVERT_NUM_MODE")
    asr_hotword_weight: int = Field(default=100, validation_alias="ASR_HOTWORD_WEIGHT")  # 1-11 普通热词；100 = 同音字替换
    asr_connect_timeout_seconds: float = Field(default=10.0, validation_alias="ASR_CONNECT_TIMEOUT_SECONDS")
    asr_final_timeout_seconds: float = Field(default=5.0, validation_alias="ASR_FINAL_TIMEOUT_SECONDS")

    # Request timing. Requests at or above this threshold are tagged as slow.
    slow_request_threshold_ms: float

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return env_settings, dotenv_settings, init_settings, file_secret_settings

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return self.db.url


_YAML_ALIASES = {
    "DATABASE_URL": "database_url_override",
    "REDIS_URL": "redis_url",
    "OPENAI_API_KEY": "openai_api_key",
    "DASHSCOPE_API_KEY": "openai_api_key",
    "OPENAI_URL": "openai_url",
    "DASHSCOPE_RESPONSES_URL": "openai_url",
    "ASR_APPID": "asr_appid",
    "ASR_SECRET_ID": "asr_secret_id",
    "ASR_SECRET_KEY": "asr_secret_key",
    "ASR_ENGINE_MODEL_TYPE": "asr_engine_model_type",
    "ASR_ENDPOINT": "asr_endpoint",
    "ASR_NEEDVAD": "asr_needvad",
    "ASR_VAD_SILENCE_TIME": "asr_vad_silence_time",
    "ASR_FILTER_MODAL": "asr_filter_modal",
    "ASR_FILTER_PUNC": "asr_filter_punc",
    "ASR_CONVERT_NUM_MODE": "asr_convert_num_mode",
    "ASR_HOTWORD_WEIGHT": "asr_hotword_weight",
    "ASR_CONNECT_TIMEOUT_SECONDS": "asr_connect_timeout_seconds",
    "ASR_FINAL_TIMEOUT_SECONDS": "asr_final_timeout_seconds",
}


def _read_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return _normalize_yaml_aliases(data)


def _normalize_yaml_aliases(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for alias, field_name in _YAML_ALIASES.items():
        if alias in normalized:
            normalized[field_name] = normalized.pop(alias)
    return normalized


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(
    config_path: Optional[Path] = None,
    defaults_path: Optional[Path] = None,
) -> Settings:
    config_dir = Path(__file__).parent.parent / "config"
    defaults = _read_yaml_config(defaults_path or config_dir / "config.defaults.yaml")
    local = _read_yaml_config(config_path or config_dir / "config.yaml")
    return Settings(**_deep_merge(defaults, local))


settings = load_settings()
