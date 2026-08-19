from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import Optional, Union
import os


class Settings(BaseSettings):
    """Application settings with environment variable support."""
    
    # Application settings
    app_name: str = "Comic Updator Backend"
    app_version: str = "1.0.0"
    debug: bool = False
    
    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000
    
    # Environment
    environment: str = "development"
    
    # Firebase settings
    firebase_project_id: Optional[str] = None
    firebase_private_key_id: Optional[str] = None
    firebase_private_key: Optional[str] = None
    firebase_client_email: Optional[str] = None
    firebase_client_id: Optional[str] = None
    firebase_auth_uri: Optional[str] = None
    firebase_token_uri: Optional[str] = None
    firebase_auth_provider_x509_cert_url: Optional[str] = None
    firebase_client_x509_cert_url: Optional[str] = None
    
    # CORS settings.
    # Typed as a union so pydantic-settings tolerates a value that is not JSON;
    # the validator below then accepts a plain comma-separated string too. A bare
    # list[str] makes a value like "https://a.com" a hard error at import time.
    cors_origins: Union[str, list[str]] = [
        "https://comic-updator-frontend.vercel.app",
    ]
    cors_methods: Union[str, list[str]] = ["*"]
    cors_headers: Union[str, list[str]] = ["*"]

    # Any localhost / 127.0.0.1 port, so local frontend dev works on whatever
    # port the dev server picks without editing this list.
    cors_origin_regex: str = r"http://(localhost|127\.0\.0\.1)(:\d+)?"

    @field_validator("cors_origins", "cors_methods", "cors_headers", mode="before")
    @classmethod
    def _split_list(cls, value):
        """Accept JSON (["a","b"]) or a comma-separated string (a,b)."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json
                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
        return value
    
    # Security
    secret_key: str = "your-secret-key-change-in-production"
    access_token_expire_minutes: int = 30

    # Background scheduler - runs once a day at this UTC time
    scheduler_enabled: bool = True
    scrape_cron_hour: int = 3
    scrape_cron_minute: int = 0
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()
