"""
Azure Managed Identity Token Manager for LiteLLM
- Lazy token fetch
- Redis-backed cache
- Strong consistency
- Safe fallback to in-memory
"""

import asyncio
import hashlib
import logging
import os
import threading
import time
from typing import Optional

from azure.identity.aio import ManagedIdentityCredential
from azure.core.credentials import AccessToken

logger = logging.getLogger(__name__)

MI_SCOPE = "https://cognitiveservices.azure.com/.default"
REFRESH_BUFFER = 300  # seconds

# =========================
# REDIS CACHE (SAFE)
# =========================

class TokenCache:
    def __init__(self):
        self.redis = None
        self.available = False
        self.mem_cache = {}
        self.mem_lock = threading.Lock()

    async def initialize(self):
        redis_host = os.getenv("REDIS_HOST")
        if not redis_host:
            logger.warning("REDIS not configured – using in-memory cache")
            return

        try:
            import redis.asyncio as redis
            self.redis = redis.Redis(
                host=redis_host,
                port=int(os.getenv("REDIS_PORT", "6379")),
                password=os.getenv("REDIS_PASSWORD"),
                ssl=os.getenv("REDIS_SSL", "false").lower() == "true",
                decode_responses=True,
            )
            await self.redis.ping()
            self.available = True
            logger.info("Redis cache enabled for MI tokens")
        except Exception as e:
            logger.warning(f"Redis unavailable, falling back to memory: {e}")

    async def get(self, key: str) -> Optional[str]:
        if self.available:
            try:
                return await self.redis.get(key)
            except Exception:
                pass

        with self.mem_lock:
            value, exp = self.mem_cache.get(key, (None, 0))
            if time.time() < exp:
                return value
            self.mem_cache.pop(key, None)
            return None

    async def set(self, key: str, value: str, ttl: int):
        if self.available:
            try:
                await self.redis.setex(key, ttl, value)
                return
            except Exception:
                pass

        with self.mem_lock:
            self.mem_cache[key] = (value, time.time() + ttl)

# =========================
# TOKEN MANAGER
# =========================

class AzureTokenManager:
    def __init__(self):
        self.cache = TokenCache()
        self.credentials = {}
        self.cred_lock = threading.Lock()

        self.loop = asyncio.new_event_loop()
        threading.Thread(
            target=self._run_loop,
            daemon=True
        ).start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def initialize(self):
        await self.cache.initialize()

    def _cred(self, client_id: str) -> ManagedIdentityCredential:
        with self.cred_lock:
            if client_id not in self.credentials:
                self.credentials[client_id] = ManagedIdentityCredential(
                    client_id=client_id
                )
            return self.credentials[client_id]

    def _key(self, client_id: str) -> str:
        h = hashlib.sha256(client_id.encode()).hexdigest()[:16]
        return f"mi_token:{h}"

    async def _fetch(self, client_id: str) -> AccessToken:
        return await self._cred(client_id).get_token(MI_SCOPE)

    async def _get(self, client_id: str) -> str:
        key = self._key(client_id)

        cached = await self.cache.get(key)
        if cached:
            return cached

        token = await self._fetch(client_id)
        ttl = max(
            int(token.expires_on - time.time()) - REFRESH_BUFFER,
            60
        )

        await self.cache.set(key, token.token, ttl)
        return token.token

    def get_token_sync(self, client_id: str) -> str:
        fut = asyncio.run_coroutine_threadsafe(
            self._get(client_id),
            self.loop
        )
        return fut.result(timeout=30)
