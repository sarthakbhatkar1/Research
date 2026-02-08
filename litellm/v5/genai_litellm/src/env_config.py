"""Environment configuration for GenAI LiteLLM Service."""
import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BlobConfig:
    """Azure Blob Storage configuration."""
    auth_type: str
    container: str
    config_blob_name: str
    account_url: Optional[str] = None
    connection_string: Optional[str] = None
    mi_client_id: Optional[str] = None

    def __post_init__(self):
        if self.auth_type == "MI" and not self.account_url:
            raise ValueError("BLOB_ACCOUNT_URL required when BLOB_AUTH_TYPE=MI")
        if self.auth_type == "CONNECTION_STRING" and not self.connection_string:
            raise ValueError("BLOB_CONNECTION_STRING required when BLOB_AUTH_TYPE=CONNECTION_STRING")


@dataclass
class RedisConfig:
    """Redis configuration (optional)."""
    enabled: bool
    host: Optional[str] = None
    port: int = 6380
    ssl: bool = True
    auth_type: Optional[str] = None
    password: Optional[str] = None
    mi_client_id: Optional[str] = None

    def __post_init__(self):
        if self.enabled and not self.host:
            raise ValueError("REDIS_HOST required when Redis is enabled")


@dataclass
class LiteLLMConfig:
    """LiteLLM configuration."""
    yaml_storage_path: str
    yaml_refresh_interval: int
    port: int
    host: str
    num_workers: int


@dataclass
class Config:
    """Complete service configuration."""
    blob: BlobConfig
    redis: RedisConfig
    litellm: LiteLLMConfig


def load_config() -> Config:
    """Load and validate configuration from environment variables."""
    
    # Blob Storage
    blob_auth_type = os.getenv("BLOB_AUTH_TYPE", "MI")
    blob_config = BlobConfig(
        auth_type=blob_auth_type,
        container=os.getenv("BLOB_DOC_CONTAINER", "litellm-config"),
        config_blob_name=os.getenv("BLOB_CONFIG_NAME", "config.yaml"),
        account_url=os.getenv("BLOB_ACCOUNT_URL"),
        connection_string=os.getenv("BLOB_CONNECTION_STRING"),
        mi_client_id=os.getenv("BLOB_MI_CLIENT_ID"),
    )

    # Redis
    redis_host = os.getenv("REDIS_HOST")
    redis_config = RedisConfig(
        enabled=bool(redis_host),
        host=redis_host,
        port=int(os.getenv("REDIS_PORT", "6380")),
        ssl=os.getenv("REDIS_SSL", "true").lower() == "true",
        auth_type=os.getenv("REDIS_AUTH_TYPE"),
        password=os.getenv("REDIS_PASSWORD"),
        mi_client_id=os.getenv("REDIS_MI_CLIENT_ID"),
    )

    # LiteLLM
    litellm_config = LiteLLMConfig(
        yaml_storage_path=os.getenv("LITELLM_YAML_STORAGE_PATH", "config.yaml"),
        yaml_refresh_interval=int(os.getenv("LITELLM_YAML_REFRESH_INTERVAL", "60")),
        port=int(os.getenv("LITELLM_PORT", "8000")),
        host=os.getenv("LITELLM_HOST", "0.0.0.0"),
        num_workers=int(os.getenv("LITELLM_NUM_WORKERS", "1")),
    )

    return Config(
        blob=blob_config,
        redis=redis_config,
        litellm=litellm_config,
    )
