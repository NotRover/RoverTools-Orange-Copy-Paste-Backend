from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/clipboard"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    jwt_private_key_path: Path = Path("./private.pem")
    jwt_public_key_path: Path = Path("./public.pem")
    jwt_algorithm: str = "RS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    # S3 / MinIO
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "clipboard-blobs"
    aws_access_key_id: str = "minioadmin"
    aws_secret_access_key: str = "minioadmin"
    aws_region: str = "us-east-1"

    # App
    app_env: str = "development"
    app_cors_origins: str = "tauri://localhost,http://localhost:1420"
    default_blob_quota_bytes: int = 524_288_000  # 500 MB

    # Email
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "Orange Clipboard <noreply@example.com>"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.app_cors_origins.split(",") if o.strip()]

    @property
    def jwt_private_key(self) -> str:
        return self.jwt_private_key_path.read_text()

    @property
    def jwt_public_key(self) -> str:
        return self.jwt_public_key_path.read_text()


settings = Settings()
