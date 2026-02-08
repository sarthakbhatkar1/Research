# main.py

from fastapi import FastAPI
from pathlib import Path
import asyncio
import logging

from blob_config_manager import BlobConfigManager
from litellm.proxy.proxy_server import initialize

logger = logging.getLogger(__name__)

app = FastAPI()

CONFIG_PATH = Path("/app/config/proxy.yaml")

blob_manager: BlobConfigManager | None = None
poll_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup():
    global blob_manager, poll_task

    blob_manager = BlobConfigManager(
        account_name=os.environ["AZURE_STORAGE_ACCOUNT_NAME"],
        container=os.environ["AZURE_BLOB_CONTAINER_NAME"],
        blob_name=os.environ["AZURE_BLOB_NAME"],
        local_config_path=CONFIG_PATH,
        poll_interval=60,
    )

    # 1️⃣ Fetch config FIRST
    await blob_manager.initialize()

    # 2️⃣ Start LiteLLM AFTER config exists
    await initialize(
        config=str(CONFIG_PATH),
        telemetry=False,
    )

    # 3️⃣ Start background poller
    poll_task = asyncio.create_task(
        blob_manager.poll_loop(on_change_callback=reload_litellm)
    )

    logger.info("✅ Startup complete")


@app.on_event("shutdown")
async def shutdown():
    if poll_task:
        poll_task.cancel()
    if blob_manager:
        await blob_manager.stop()
