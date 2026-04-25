from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8080
    db_path: str = "./data/usage.db"
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
