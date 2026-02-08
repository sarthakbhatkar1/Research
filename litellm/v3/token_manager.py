import asyncio
import logging
import os
import threading
import time
import hashlib
from typing import Optional

from azure.identity.aio import ManagedIdentityCredential
import redis.asyncio as redis
from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("mi-token")


class AzureMITokenManager(CustomLogger):
    SCOPE = "https://cognitiveservices.azure.com/.default"

    def __init__(self):
        super().__init__()
        self.redis = None
        self.memory_cache = {}
        self.lock = threading.Lock()
        self.creds = {}

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    async def initialize(self):
        host = os.getenv("REDIS_HOST")
        if not host:
            logger.warning("Redis not configured, using memory cache")
            return

        self.redis = redis.Redis(
            host=host,
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD"),
            ssl=os.getenv("REDIS_SSL", "true").lower() == "true",
        )

        try:
            await self.redis.ping()
            logger.info("Redis connected for MI token cache")
        except Exception:
            logger.warning("Redis unavailable, falling back to memory")
            self.redis = None

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        params = data.get("litellm_params", {})
        client_id = params.get("azure_mi_client_id")
        if not client_id:
            return data

        token = await self._get_token(client_id)
        params["api_key"] = token
        return data

    async def _get_token(self, client_id: str) -> str:
        key = f"mi:{hashlib.sha256(client_id.encode()).hexdigest()[:12]}"

        if self.redis:
            tok = await self.redis.get(key)
            if tok:
                return tok

        cached = self.memory_cache.get(key)
        if cached and cached[1] > time.time():
            return cached[0]

        cred = self.creds.get(client_id)
        if not cred:
            cred = ManagedIdentityCredential(client_id=client_id)
            self.creds[client_id] = cred

        token = await cred.get_token(self.SCOPE)
        ttl = token.expires_on - int(time.time()) - 300
        ttl = max(ttl, 60)

        if self.redis:
            await self.redis.setex(key, ttl, token.token)

        self.memory_cache[key] = (token.token, time.time() + ttl)
        return token.token

    async def close(self):
        if self.redis:
            await self.redis.close()
        for c in self.creds.values():
            await c.close()
