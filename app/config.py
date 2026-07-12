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

    # Apple Push Notification service. Push is disabled when credentials are absent.
    apns_team_id: Optional[str] = Field(default=None, validation_alias="APNS_TEAM_ID")
    apns_key_id: Optional[str] = Field(default=None, validation_alias="APNS_KEY_ID")
    # Resolved PEM content. Prefer setting apns_private_key_path (or a filename in
    # apns_private_key) so the .p8 stays as a sibling of config.yaml.
    apns_private_key: Optional[str] = Field(default=None, validation_alias="APNS_PRIVATE_KEY")
    apns_private_key_path: Optional[str] = Field(default=None, validation_alias="APNS_PRIVATE_KEY_PATH")
    apns_bundle_id: str = Field(default="com.yhma.Evenly", validation_alias="APNS_BUNDLE_ID")

    # Public web origin used for QR / Universal Link invite URLs (landing site).
    public_app_base_url: str = Field(
        default="https://app.ismyh.cn",
        validation_alias="PUBLIC_APP_BASE_URL",
    )

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
    "APNS_TEAM_ID": "apns_team_id",
    "APNS_KEY_ID": "apns_key_id",
    "APNS_PRIVATE_KEY": "apns_private_key",
    "APNS_PRIVATE_KEY_PATH": "apns_private_key_path",
    "APNS_BUNDLE_ID": "apns_bundle_id",
    "PUBLIC_APP_BASE_URL": "public_app_base_url",
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


def _looks_like_pem_private_key(value: str) -> bool:
    return "BEGIN PRIVATE KEY" in value or "BEGIN EC PRIVATE KEY" in value


def _resolve_path(path_value: str, config_dir: Path) -> Path:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    # Prefer relative-to-config-dir (where config.yaml and AuthKey_*.p8 live).
    candidate = config_dir / path
    if candidate.exists():
        return candidate
    # Fall back to CWD-relative paths for local tooling.
    return path


def _resolve_apns_private_key(merged: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Load APNs .p8 content from a path sibling to config.yaml when needed.

    Resolution order:
    1. Inline PEM in apns_private_key / APNS_PRIVATE_KEY
    2. Explicit apns_private_key_path / APNS_PRIVATE_KEY_PATH
    3. apns_private_key value treated as a filename/path (e.g. AuthKey_xxx.p8)
    """
    resolved = dict(merged)
    inline = resolved.get("apns_private_key")
    if isinstance(inline, str) and inline.strip() and _looks_like_pem_private_key(inline):
        resolved["apns_private_key"] = inline.replace("\\n", "\n").strip()
        return resolved

    path_value = resolved.get("apns_private_key_path")
    if not path_value and isinstance(inline, str) and inline.strip():
        path_value = inline.strip()

    if not path_value:
        # Empty yaml key (null/"") should not keep a falsey placeholder.
        if "apns_private_key" in resolved and not resolved.get("apns_private_key"):
            resolved["apns_private_key"] = None
        return resolved

    key_path = _resolve_path(str(path_value), config_dir)
    if not key_path.is_file():
        raise FileNotFoundError(
            f"APNs private key file not found: {key_path} "
            f"(set apns_private_key_path next to config.yaml)"
        )
    resolved["apns_private_key"] = key_path.read_text(encoding="utf-8").strip()
    resolved["apns_private_key_path"] = str(key_path)
    return resolved


def _finalize_apns_private_key(loaded: Settings, config_dir: Path) -> Settings:
    """Ensure apns_private_key holds PEM content after env/yaml merge."""
    current = loaded.apns_private_key
    if isinstance(current, str) and current.strip() and _looks_like_pem_private_key(current):
        normalized = current.replace("\\n", "\n").strip()
        if normalized == current:
            return loaded
        return loaded.model_copy(update={"apns_private_key": normalized})

    path_value = loaded.apns_private_key_path
    if not path_value and isinstance(current, str) and current.strip():
        path_value = current.strip()
    if not path_value:
        if current in ("", None):
            return loaded.model_copy(update={"apns_private_key": None})
        return loaded

    key_path = _resolve_path(path_value, config_dir)
    if not key_path.is_file():
        raise FileNotFoundError(
            f"APNs private key file not found: {key_path} "
            f"(set apns_private_key_path next to config.yaml)"
        )
    return loaded.model_copy(
        update={
            "apns_private_key": key_path.read_text(encoding="utf-8").strip(),
            "apns_private_key_path": str(key_path),
        }
    )


def load_settings(
    config_path: Optional[Path] = None,
    defaults_path: Optional[Path] = None,
) -> Settings:
    config_dir = Path(__file__).parent.parent / "config"
    resolved_config_path = config_path or config_dir / "config.yaml"
    # Paths like AuthKey_*.p8 are resolved relative to the directory that holds config.yaml.
    key_base_dir = resolved_config_path.parent if resolved_config_path else config_dir
    defaults = _read_yaml_config(defaults_path or config_dir / "config.defaults.yaml")
    local = _read_yaml_config(resolved_config_path)
    merged = _resolve_apns_private_key(_deep_merge(defaults, local), key_base_dir)
    return _finalize_apns_private_key(Settings(**merged), key_base_dir)


settings = load_settings()
