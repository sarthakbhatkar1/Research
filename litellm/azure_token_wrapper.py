"""
Azure Managed Identity Token Manager
- Lazy token fetching (only when needed)
- Redis-backed caching with single-flight behavior
- Automatic fallback to in-memory cache
- Thread-safe synchronous wrapper for LiteLLM
"""

import asyncio
import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from azure.core.credentials import AccessToken
from azure.identity.aio import ManagedIdentityCredential

logger = logging.getLogger(__name__)


# ============================================================================
# REDIS CLIENT (with graceful fallback)
# ============================================================================

class RedisClient:
    """
    Redis client wrapper with automatic fallback to in-memory cache
    
    Handles:
    - Connection failures
    - Missing configuration
    - Token stampede prevention
    """
    
    def __init__(self, host: Optional[str], port: int, password: Optional[str], ssl: bool):
        self.host = host
        self.port = port
        self.password = password
        self.ssl = ssl
        self.redis = None
        self.available = False
        
        # In-memory fallback
        self.memory_cache = {}
        self.memory_cache_lock = threading.Lock()
    
    async def initialize(self):
        """Initialize Redis connection with fallback"""
        if not self.host:
            logger.info("Redis not configured, using in-memory token cache")
            self.available = False
            return
        
        try:
            import redis.asyncio as redis
            
            self.redis = redis.Redis(
                host=self.host,
                port=self.port,
                password=self.password,
                ssl=self.ssl,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
            
            # Test connection
            await self.redis.ping()
            self.available = True
            logger.info(f"✅ Redis connected: {self.host}:{self.port}")
            
        except Exception as e:
            logger.warning(f"Redis unavailable, using in-memory cache: {e}")
            self.available = False
            self.redis = None
    
    async def get(self, key: str) -> Optional[str]:
        """Get value from Redis or in-memory cache"""
        if self.available and self.redis:
            try:
                return await self.redis.get(key)
            except Exception as e:
                logger.warning(f"Redis GET failed: {e}")
                # Fall through to memory cache
        
        # In-memory fallback
        with self.memory_cache_lock:
            entry = self.memory_cache.get(key)
            if entry:
                value, expiry = entry
                if time.time() < expiry:
                    return value
                else:
                    del self.memory_cache[key]
        
        return None
    
    async def setex(self, key: str, seconds: int, value: str) -> bool:
        """Set value with expiration in Redis or in-memory cache"""
        if self.available and self.redis:
            try:
                await self.redis.setex(key, seconds, value)
                return True
            except Exception as e:
                logger.warning(f"Redis SETEX failed: {e}")
                # Fall through to memory cache
        
        # In-memory fallback
        with self.memory_cache_lock:
            expiry = time.time() + seconds
            self.memory_cache[key] = (value, expiry)
        
        return True
    
    async def setnx(self, key: str, value: str) -> bool:
        """
        Set if not exists (for locking)
        
        Returns:
            True if key was set, False if already exists
        """
        if self.available and self.redis:
            try:
                return await self.redis.setnx(key, value)
            except Exception as e:
                logger.warning(f"Redis SETNX failed: {e}")
        
        # In-memory fallback (simple, not distributed)
        with self.memory_cache_lock:
            if key in self.memory_cache:
                value_stored, expiry = self.memory_cache[key]
                if time.time() < expiry:
                    return False
            
            # Set with short expiry for lock
            self.memory_cache[key] = (value, time.time() + 10)
            return True
    
    async def delete(self, key: str):
        """Delete key from Redis or in-memory cache"""
        if self.available and self.redis:
            try:
                await self.redis.delete(key)
                return
            except Exception as e:
                logger.warning(f"Redis DELETE failed: {e}")
        
        # In-memory fallback
        with self.memory_cache_lock:
            self.memory_cache.pop(key, None)
    
    async def close(self):
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()


# ============================================================================
# TOKEN MANAGER
# ============================================================================

class AzureTokenManager:
    """
    Manages Azure Managed Identity tokens with caching
    
    Features:
    - Lazy token fetching (only when model is called)
    - Redis caching with automatic refresh
    - Single-flight token requests (stampede prevention)
    - Synchronous wrapper for LiteLLM callbacks
    - Automatic expiry handling
    """
    
    SCOPE = "https://cognitiveservices.azure.com/.default"
    TOKEN_BUFFER_SECONDS = 300  # Refresh 5 minutes before expiry
    
    def __init__(self):
        self.redis: Optional[RedisClient] = None
        self.credentials = {}  # Cache credential objects per client_id
        self.credentials_lock = threading.Lock()
        
        # Event loop for async operations in sync context
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
    
    async def initialize(self):
        """Initialize Redis client and event loop"""
        from litellm_proxy_runner import Config
        
        # Initialize Redis
        self.redis = RedisClient(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            password=Config.REDIS_PASSWORD,
            ssl=Config.REDIS_SSL
        )
        await self.redis.initialize()
        
        # Start event loop in background thread for sync operations
        self._start_background_loop()
    
    def _start_background_loop(self):
        """Start a background event loop for sync token fetching"""
        def run_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
        
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=run_loop, args=(self.loop,), daemon=True)
        self.loop_thread.start()
        logger.info("Background event loop started for token operations")
    
    def _get_credential(self, client_id: str) -> ManagedIdentityCredential:
        """Get or create a credential object for a client_id"""
        with self.credentials_lock:
            if client_id not in self.credentials:
                self.credentials[client_id] = ManagedIdentityCredential(
                    client_id=client_id
                )
                logger.debug(f"Created credential for client_id={client_id}")
            return self.credentials[client_id]
    
    def _cache_key(self, client_id: str) -> str:
        """Generate cache key for a client_id"""
        # Use hash to avoid storing client_id directly in Redis key
        hash_suffix = hashlib.sha256(client_id.encode()).hexdigest()[:16]
        return f"mi_token:{hash_suffix}"
    
    def _lock_key(self, client_id: str) -> str:
        """Generate lock key for single-flight requests"""
        cache_key = self._cache_key(client_id)
        return f"{cache_key}:lock"
    
    async def _fetch_token_from_mi(self, client_id: str) -> AccessToken:
        """Fetch token from Managed Identity"""
        credential = self._get_credential(client_id)
        
        logger.info(f"Fetching new token from Managed Identity for client_id={client_id}")
        token = await credential.get_token(self.SCOPE)
        
        return token
    
    async def get_token(self, client_id: str) -> str:
        """
        Get cached token or fetch new one
        
        Process:
        1. Check cache
        2. If expired/missing, acquire lock
        3. Double-check cache (another thread may have fetched)
        4. Fetch from MI
        5. Cache with TTL
        6. Release lock
        
        Returns:
            Access token string
        """
        cache_key = self._cache_key(client_id)
        lock_key = self._lock_key(client_id)
        
        # Try cache first
        cached_token = await self.redis.get(cache_key)
        if cached_token:
            logger.debug(f"Token cache hit for client_id={client_id}")
            return cached_token
        
        logger.debug(f"Token cache miss for client_id={client_id}")
        
        # Single-flight: try to acquire lock
        lock_acquired = await self.redis.setnx(lock_key, "locked")
        
        if not lock_acquired:
            # Another thread is fetching, wait and retry from cache
            logger.debug(f"Waiting for token fetch by another thread (client_id={client_id})")
            await asyncio.sleep(0.5)
            
            # Try cache again
            cached_token = await self.redis.get(cache_key)
            if cached_token:
                return cached_token
            
            # Still not there, fall through to fetch ourselves
            logger.warning(f"Lock holder didn't populate cache, fetching anyway")
        
        try:
            # Double-check cache (race condition)
            cached_token = await self.redis.get(cache_key)
            if cached_token:
                return cached_token
            
            # Fetch new token
            access_token = await self._fetch_token_from_mi(client_id)
            token_str = access_token.token
            
            # Calculate TTL with buffer
            now = datetime.now()
            expiry_time = datetime.fromtimestamp(access_token.expires_on)
            ttl_seconds = int((expiry_time - now).total_seconds()) - self.TOKEN_BUFFER_SECONDS
            
            if ttl_seconds < 60:
                ttl_seconds = 60  # Minimum 1 minute cache
            
            # Cache token
            await self.redis.setex(cache_key, ttl_seconds, token_str)
            logger.info(f"Token cached for {ttl_seconds}s (expires at {expiry_time.isoformat()})")
            
            return token_str
            
        finally:
            # Release lock
            if lock_acquired:
                await self.redis.delete(lock_key)
    
    def get_token_sync(self, client_id: str) -> str:
        """
        Synchronous wrapper for get_token
        
        This is called by LiteLLM's azure_ad_token callable.
        We use asyncio.run_coroutine_threadsafe to bridge async -> sync.
        """
        if not self.loop:
            raise RuntimeError("Token manager not initialized")
        
        # Schedule coroutine in background loop
        future = asyncio.run_coroutine_threadsafe(
            self.get_token(client_id),
            self.loop
        )
        
        # Wait for result (with timeout)
        try:
            token = future.result(timeout=30)
            return token
        except Exception as e:
            logger.error(f"Failed to get token for client_id={client_id}: {e}")
            raise
    
    async def close(self):
        """Cleanup resources"""
        # Close all credentials
        with self.credentials_lock:
            for credential in self.credentials.values():
                await credential.close()
            self.credentials.clear()
        
        # Close Redis
        if self.redis:
            await self.redis.close()
        
        # Stop background loop
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
