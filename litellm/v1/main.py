import logging
import os

import litellm
from fastapi import FastAPI
from litellm.proxy.proxy_server import app, initialize
from litellm.integrations.custom_logger import CustomLogger

from azure_token_wrapper import AzureTokenManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================
# MI TOKEN INJECTOR
# =========================

class MITokenInjector(CustomLogger):
    def __init__(self, manager: AzureTokenManager):
        self.manager = manager
        super().__init__()

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        params = data.get("litellm_params", {})
        client_id = params.get("azure_mi_client_id")
        if client_id:
            token = self.manager.get_token_sync(client_id)
            params["api_key"] = token
        return data

# =========================
# STARTUP
# =========================

token_manager = AzureTokenManager()

@app.on_event("startup")
async def startup():
    """
    CRITICAL:
    - LiteLLM fetches blob config BEFORE initialize()
    - We DO NOT touch config files
    """
    logger.info("Starting LiteLLM Proxy (blob-backed config)")

    await token_manager.initialize()

    litellm.callbacks = [
        MITokenInjector(token_manager)
    ]

    await initialize(
        telemetry=False
    )

    logger.info("LiteLLM Proxy ready")

# =========================
# ENTRYPOINT
# =========================

def main():
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info"
    )

if __name__ == "__main__":
    main()
