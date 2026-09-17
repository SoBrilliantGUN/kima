from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/ 目录（相对本文件向上三级：core -> app -> backend）
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "kima"
    environment: str = "development"
    debug: bool = True
    api_prefix: str = "/api"

    # Database
    database_url: str = "postgresql+asyncpg://kima:kima@localhost:5432/kima"

    # LLM（DeepSeek，OpenAI 兼容）
    llm_provider: str = "fake"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"

    # Embedding（SiliconFlow）
    embedding_provider: str = "fake"
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # MinerU
    mineru_api_base_url: str = "https://mineru.net"
    mineru_api_token: str = ""

    # CORS
    # NoDecode：env 里是逗号分隔字符串，跳过 pydantic-settings 的 JSON 解码，交给下方 _split_cors
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
