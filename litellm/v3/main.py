import asyncio
import logging
import os
import shutil
import yaml
from pathlib import Path
from typing import Optional

import litellm
from litellm.proxy.proxy_server import app, initialize

from blob_config import BlobConfigManager
from token_manager import AzureMITokenManager

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("genai-litellm")

# -----------------------------------------------------------------------------
# PATHS
# -----------------------------------------------------------------------------
CONFIG_DIR = Path("litellm")
ACTIVE_CONFIG = CONFIG_DIR / "config.yaml"
LAST_GOOD_CONFIG = CONFIG_DIR / "config.last_good.yaml"

# -----------------------------------------------------------------------------
# GLOBALS
# -----------------------------------------------------------------------------
blob_manager: Optional[BlobConfigManager] = None
token_manager: Optional[AzureMITokenManager] = None
litellm_started = False


# -----------------------------------------------------------------------------
# STARTUP
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    global blob_manager, token_manager, litellm_started

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Starting GenAI LiteLLM service")

    # Token manager (lazy, safe)
    token_manager = AzureMITokenManager()
    await token_manager.initialize()

    # Register callback
    litellm.callbacks = [token_manager]

    # Blob config manager
    blob_manager = BlobConfigManager(
        active_path=ACTIVE_CONFIG,
        last_good_path=LAST_GOOD_CONFIG,
    )

    # Background loop
    asyncio.create_task(config_bootstrap_loop())


async def config_bootstrap_loop():
    """
    This loop NEVER exits.
    It guarantees:
    - Initial config fetch
    - Retry on failure
    - Hot reload
    """
    global litellm_started

    interval = int(os.getenv("LITELLM_YAML_REFRESH_INTERVAL", "60"))

    while True:
        try:
            changed = await blob_manager.sync_from_blob()

            if not litellm_started and ACTIVE_CONFIG.exists():
                logger.info("Initial config ready, starting LiteLLM proxy")

                await initialize(
                    config=str(ACTIVE_CONFIG),
                    telemetry=False,
                )

                litellm_started = True
                logger.info("LiteLLM proxy started successfully")

            elif litellm_started and changed:
                logger.info("Config updated, reloading LiteLLM")
                await initialize(
                    config=str(ACTIVE_CONFIG),
                    telemetry=False,
                )

        except Exception as e:
            logger.error(f"Config loop error: {e}", exc_info=True)

        await asyncio.sleep(interval)


# -----------------------------------------------------------------------------
# SHUTDOWN
# -----------------------------------------------------------------------------
@app.on_event("shutdown")
async def shutdown():
    logger.info("Shutting down GenAI LiteLLM service")
    if token_manager:
        await token_manager.close()


# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
def main():
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
