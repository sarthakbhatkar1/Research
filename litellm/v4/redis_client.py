"""
Resilient Redis client with automatic fallback to in-memory cache.
Handles both password and Managed Identity authentication.

This Redis layer is used for:
1. Caching Managed Identity tokens (if needed by custom logic)
2. General application-level caching
3. Metrics and observability data

Note: LiteLLM handles its own internal token caching, so this is 
supplementary infrastructure for additional caching needs.
"""
import logging
import threading
import time
from typing import Optional, Dict
from datetime import timedelta

logger = logging.getLogger(__name__)


class ResilientRedisClient:
    """
    Redis client that never crashes the service.
    Falls back to in-memory cache if Redis is unavailable.
    """

    def __init__(self, config):
        """
        Initialize Redis client with optional MI or password auth.
        
        Args:
            config: RedisConfig from env_config.py
        """
        self.config = config
        self._redis_client = None
        self._memory_cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()
        self._using_fallback = False

        if not config.enabled:
            logger.info("Redis disabled - using in-memory cache only")
            self._using_fallback = True
            return

        self._initialize_redis()

    def _initialize_redis(self):
        """Initialize Redis connection with resilience."""
        try:
            import redis

            # Build connection kwargs
            conn_kwargs = {
                "host": self.config.host,
                "port": self.config.port,
                "ssl": self.config.ssl,
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
                "retry_on_timeout": True,
                "decode_responses": True,
            }

            # Handle authentication
            if self.config.auth_type == "PASSWORD":
                conn_kwargs["password"] = self.config.password
            elif self.config.auth_type == "MI":
                # Use Azure Identity for MI-based auth
                from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
                
                if self.config.mi_client_id:
                    credential = ManagedIdentityCredential(client_id=self.config.mi_client_id)
                else:
                    credential = DefaultAzureCredential()
                
                # Get token for Azure Cache for Redis
                token_response = credential.get_token("https://redis.azure.com/.default")
                conn_kwargs["password"] = token_response.token
                
                logger.info("Redis: Using Managed Identity authentication")

            # Create client with connection pool
            self._redis_client = redis.Redis(**conn_kwargs)

            # Test connection
            self._redis_client.ping()
            logger.info(f"Redis connected: {self.config.host}:{self.config.port}")
            self._using_fallback = False

        except ImportError:
            logger.warning("Redis library not installed - using in-memory cache")
            self._using_fallback = True
        except Exception as e:
            logger.warning(f"Redis connection failed: {e} - using in-memory cache")
            self._using_fallback = True

    def get(self, key: str) -> Optional[str]:
        """
        Get value from cache (Redis or memory fallback).
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None
        """
        # Try Redis first
        if not self._using_fallback and self._redis_client:
            try:
                value = self._redis_client.get(key)
                return value
            except Exception as e:
                logger.warning(f"Redis GET failed for key '{key}': {e} - checking memory cache")
                # Fall through to memory cache

        # Memory cache fallback
        with self._cache_lock:
            return self._memory_cache.get(key)

    def set(self, key: str, value: str, ttl_seconds: int = 3600) -> bool:
        """
        Set value in cache with TTL (Redis or memory fallback).
        
        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Time-to-live in seconds
            
        Returns:
            True if successful, False otherwise
        """
        success = False

        # Try Redis first
        if not self._using_fallback and self._redis_client:
            try:
                self._redis_client.setex(key, timedelta(seconds=ttl_seconds), value)
                success = True
            except Exception as e:
                logger.warning(f"Redis SET failed for key '{key}': {e} - using memory cache")

        # Always update memory cache as backup
        with self._cache_lock:
            self._memory_cache[key] = value
            # Note: In-memory cache doesn't enforce TTL - acceptable for this use case

        return success

    def delete(self, key: str) -> bool:
        """
        Delete key from cache.
        
        Args:
            key: Cache key to delete
            
        Returns:
            True if successful
        """
        success = False

        if not self._using_fallback and self._redis_client:
            try:
                self._redis_client.delete(key)
                success = True
            except Exception as e:
                logger.warning(f"Redis DELETE failed for key '{key}': {e}")

        # Also remove from memory cache
        with self._cache_lock:
            self._memory_cache.pop(key, None)

        return success

    def is_redis_available(self) -> bool:
        """Check if Redis is currently available."""
        if self._using_fallback or not self._redis_client:
            return False

        try:
            self._redis_client.ping()
            return True
        except Exception:
            return False

    def health_check(self) -> Dict[str, any]:
        """
        Perform health check and return status.
        
        Returns:
            Dict with health status information
        """
        status = {
            "redis_enabled": self.config.enabled,
            "redis_available": False,
            "using_fallback": self._using_fallback,
            "memory_cache_size": 0,
        }

        if self.config.enabled and not self._using_fallback:
            try:
                self._redis_client.ping()
                info = self._redis_client.info("stats")
                status["redis_available"] = True
                status["redis_connections"] = info.get("total_connections_received", 0)
            except Exception as e:
                logger.warning(f"Redis health check failed: {e}")

        with self._cache_lock:
            status["memory_cache_size"] = len(self._memory_cache)

        return status

    def get_stats(self) -> Dict[str, any]:
        """Get Redis statistics (if available)."""
        if not self.is_redis_available():
            return {"error": "Redis not available"}

        try:
            info = self._redis_client.info()
            return {
                "connected_clients": info.get("connected_clients", 0),
                "used_memory_human": info.get("used_memory_human", "N/A"),
                "total_commands_processed": info.get("total_commands_processed", 0),
                "keyspace_hits": info.get("keyspace_hits", 0),
                "keyspace_misses": info.get("keyspace_misses", 0),
            }
        except Exception as e:
            logger.error(f"Failed to get Redis stats: {e}")
            return {"error": str(e)}
