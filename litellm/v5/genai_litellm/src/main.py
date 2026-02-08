"""
GenAI LiteLLM Service - Main Entrypoint

Production-grade LLM inference with:
- Multiple Managed Identities (per-region)
- Azure Blob Storage config management
- Redis caching with fallback
- Databricks SPN support
- Atomic config updates
"""
import logging
import sys
import os
import subprocess

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Import our modules
from env_config import load_config
from redis_client import ResilientRedisClient
from blob_manager import BlobConfigManager
from config_daemon import ConfigRefreshDaemon, initial_config_fetch, setup_signal_handlers


def validate_environment():
    """Validate required dependencies."""
    logger.info("=" * 60)
    logger.info("ENVIRONMENT VALIDATION")
    logger.info("=" * 60)
    
    required = ["litellm", "azure.storage.blob", "azure.identity", "yaml"]
    missing = []
    
    for pkg in required:
        try:
            __import__(pkg.replace("-", "_").split(".")[0])
            logger.info(f"✓ {pkg}")
        except ImportError:
            logger.error(f"✗ {pkg}")
            missing.append(pkg)
    
    if missing:
        logger.error(f"Missing packages: {', '.join(missing)}")
        sys.exit(1)
    
    logger.info("=" * 60)


def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("GenAI LiteLLM Service - Starting")
    logger.info("=" * 60)
    
    try:
        # 1. Validate environment
        validate_environment()
        
        # 2. Load config
        logger.info("Loading configuration...")
        config = load_config()
        logger.info("✓ Configuration loaded")
        
        # 3. Initialize Redis (optional)
        logger.info("=" * 60)
        logger.info("REDIS INITIALIZATION")
        logger.info("=" * 60)
        redis_client = ResilientRedisClient(config.redis)
        health = redis_client.health_check()
        if health['using_fallback']:
            logger.warning("⚠ Using in-memory cache (Redis unavailable)")
        else:
            logger.info("✓ Redis connected")
        logger.info("=" * 60)
        
        # 4. Initialize blob manager
        logger.info("BLOB STORAGE INITIALIZATION")
        logger.info("=" * 60)
        blob_manager = BlobConfigManager(config.blob)
        logger.info("=" * 60)
        
        # 5. Get local config path
        local_config_path = os.path.abspath(config.litellm.yaml_storage_path)
        logger.info(f"Local config path: {local_config_path}")
        
        # 6. Initial config fetch (BLOCKING)
        initial_config_fetch(
            blob_manager=blob_manager,
            local_config_path=local_config_path,
            refresh_interval=config.litellm.yaml_refresh_interval
        )
        
        # 7. Start config refresh daemon
        logger.info("=" * 60)
        logger.info("CONFIG REFRESH DAEMON")
        logger.info("=" * 60)
        refresh_daemon = ConfigRefreshDaemon(
            blob_manager=blob_manager,
            local_config_path=local_config_path,
            refresh_interval=config.litellm.yaml_refresh_interval
        )
        refresh_daemon.start()
        setup_signal_handlers(refresh_daemon)
        logger.info("=" * 60)
        
        # 8. Start LiteLLM server (BLOCKS)
        logger.info("STARTING LITELLM SERVER")
        logger.info("=" * 60)
        logger.info(f"Config: {local_config_path}")
        logger.info(f"Host: {config.litellm.host}")
        logger.info(f"Port: {config.litellm.port}")
        logger.info("=" * 60)
        
        cmd = [
            "litellm",
            "--config", local_config_path,
            "--host", config.litellm.host,
            "--port", str(config.litellm.port),
            "--num_workers", str(config.litellm.num_workers),
        ]
        
        logger.info(f"Command: {' '.join(cmd)}")
        logger.info("=" * 60)
        
        # Start LiteLLM - this blocks
        subprocess.run(cmd, check=True)
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
