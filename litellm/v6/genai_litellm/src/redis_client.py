"""Resilient Redis client with automatic in-memory fallback."""
import logging
import threading
from typing import Optional, Dict
from datetime import timedelta

logger = logging.getLogger(__name__)


class ResilientRedisClient:
    """
    Redis client that gracefully falls back to in-memory cache.
    Never crashes the service.
    """

    def __init__(self, config):
        self.config = config
        self._redis_client = None
        self._memory_cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()
        self._using_fallback = False

        if not config.enabled:
            logger.info("Redis disabled - using in-memory cache")
            self._using_fallback = True
            return

        self._initialize_redis()

    def _initialize_redis(self):
        """Initialize Redis connection with resilience."""
        try:
            import redis
            from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

            conn_kwargs = {
                "host": self.config.host,
                "port": self.config.port,
                "ssl": self.config.ssl,
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
                "decode_responses": True,
            }

            # Handle authentication
            if self.config.auth_type == "PASSWORD":
                conn_kwargs["password"] = self.config.password
                logger.info("Redis: Using password authentication")
            elif self.config.auth_type == "MI":
                if self.config.mi_client_id:
                    credential = ManagedIdentityCredential(client_id=self.config.mi_client_id)
                else:
                    credential = DefaultAzureCredential()
                
                token_response = credential.get_token("https://redis.azure.com/.default")
                conn_kwargs["password"] = token_response.token
                logger.info("Redis: Using Managed Identity authentication")

            self._redis_client = redis.Redis(**conn_kwargs)
            self._redis_client.ping()
            logger.info(f"✓ Redis connected: {self.config.host}:{self.config.port}")
            self._using_fallback = False

        except ImportError as e:
            logger.warning(f"Redis library not available: {e} - using in-memory cache")
            self._using_fallback = True
        except Exception as e:
            logger.warning(f"Redis connection failed: {e} - using in-memory cache")
            self._using_fallback = True

    def get(self, key: str) -> Optional[str]:
        """Get value from cache."""
        if not self._using_fallback and self._redis_client:
            try:
                return self._redis_client.get(key)
            except Exception as e:
                logger.warning(f"Redis GET failed: {e}")
        
        with self._cache_lock:
            return self._memory_cache.get(key)

    def set(self, key: str, value: str, ttl_seconds: int = 3600) -> bool:
        """Set value in cache with TTL."""
        if not self._using_fallback and self._redis_client:
            try:
                self._redis_client.setex(key, timedelta(seconds=ttl_seconds), value)
            except Exception as e:
                logger.warning(f"Redis SET failed: {e}")

        # Always update memory cache as backup
        with self._cache_lock:
            self._memory_cache[key] = value
        
        return True

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if not self._using_fallback and self._redis_client:
            try:
                self._redis_client.delete(key)
            except Exception as e:
                logger.warning(f"Redis DELETE failed: {e}")
        
        with self._cache_lock:
            self._memory_cache.pop(key, None)
        
        return True

    def health_check(self) -> Dict:
        """Get health status."""
        status = {
            "redis_enabled": self.config.enabled,
            "redis_available": False,
            "using_fallback": self._using_fallback,
        }

        if not self._using_fallback and self._redis_client:
            try:
                self._redis_client.ping()
                status["redis_available"] = True
            except Exception:
                pass

        return status
