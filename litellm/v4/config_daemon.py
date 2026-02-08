"""
Config refresh daemon - background thread that periodically fetches config from blob.
Runs independently of LiteLLM server lifecycle.
"""
import logging
import threading
import time
import signal
import sys

logger = logging.getLogger(__name__)


class ConfigRefreshDaemon:
    """
    Background daemon that periodically refreshes config.yaml from blob storage.
    
    Features:
    - Non-blocking: Never crashes the service
    - Atomic updates: Uses temp file -> rename pattern
    - Validation: Only applies valid configs
    - Resilient: Keeps retrying on failure
    """

    def __init__(self, blob_manager, local_config_path: str, refresh_interval: int):
        """
        Initialize config refresh daemon.
        
        Args:
            blob_manager: BlobConfigManager instance
            local_config_path: Path to local config.yaml
            refresh_interval: Refresh interval in seconds
        """
        self.blob_manager = blob_manager
        self.local_config_path = local_config_path
        self.refresh_interval = refresh_interval
        self._stop_event = threading.Event()
        self._thread = None
        self._last_refresh_success = False
        self._refresh_count = 0
        self._failure_count = 0

    def start(self):
        """Start the refresh daemon in a background thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Config refresh daemon already running")
            return

        self._thread = threading.Thread(target=self._refresh_loop, daemon=True, name="ConfigRefreshDaemon")
        self._thread.start()
        logger.info(f"✓ Config refresh daemon started (interval: {self.refresh_interval}s)")

    def stop(self):
        """Stop the refresh daemon gracefully."""
        logger.info("Stopping config refresh daemon...")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("✓ Config refresh daemon stopped")

    def _refresh_loop(self):
        """Main refresh loop - runs in background thread."""
        logger.info("Config refresh loop started")

        while not self._stop_event.is_set():
            try:
                # Attempt to fetch and update config
                success = self.blob_manager.fetch_config(self.local_config_path)

                if success:
                    # Validate the updated config
                    if self.blob_manager.validate_config_file(self.local_config_path):
                        self._refresh_count += 1
                        self._last_refresh_success = True
                        logger.info(f"✓ Config refreshed successfully (count: {self._refresh_count})")
                    else:
                        self._failure_count += 1
                        logger.error("Config validation failed - keeping previous config")
                else:
                    self._failure_count += 1
                    self._last_refresh_success = False
                    logger.warning(f"Config fetch failed (failures: {self._failure_count})")

            except Exception as e:
                self._failure_count += 1
                self._last_refresh_success = False
                logger.error(f"Config refresh error: {e}")

            # Wait for next refresh interval (or until stop event)
            self._stop_event.wait(self.refresh_interval)

        logger.info("Config refresh loop exited")

    def get_stats(self) -> dict:
        """Get refresh daemon statistics."""
        return {
            "refresh_interval_seconds": self.refresh_interval,
            "total_refreshes": self._refresh_count,
            "total_failures": self._failure_count,
            "last_refresh_success": self._last_refresh_success,
            "daemon_running": self._thread is not None and self._thread.is_alive(),
        }


def initial_config_fetch(blob_manager, local_config_path: str, refresh_interval: int) -> bool:
    """
    Perform initial config fetch with blocking retries.
    
    This runs BEFORE starting the LiteLLM server to ensure we have a valid config.
    
    Args:
        blob_manager: BlobConfigManager instance
        local_config_path: Path to local config.yaml
        refresh_interval: Retry interval in seconds (reused from refresh_interval)
        
    Returns:
        True when a valid config is successfully fetched
    """
    logger.info("=" * 60)
    logger.info("INITIAL CONFIG FETCH")
    logger.info("=" * 60)

    attempt = 0
    while True:
        attempt += 1
        logger.info(f"Attempt {attempt}: Fetching config from blob storage...")

        success = blob_manager.fetch_config(local_config_path)

        if success:
            # Validate the config
            if blob_manager.validate_config_file(local_config_path):
                logger.info("✓ Initial config fetch successful!")
                logger.info("=" * 60)
                return True
            else:
                logger.error("✗ Config validation failed")
        else:
            logger.error("✗ Config fetch failed")

        logger.warning(f"Retrying in {refresh_interval} seconds... (Ctrl+C to abort)")
        time.sleep(refresh_interval)


def setup_signal_handlers(daemon: ConfigRefreshDaemon):
    """
    Setup graceful shutdown signal handlers.
    
    Args:
        daemon: ConfigRefreshDaemon instance to stop on shutdown
    """
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info("✓ Signal handlers configured (SIGINT, SIGTERM)")
