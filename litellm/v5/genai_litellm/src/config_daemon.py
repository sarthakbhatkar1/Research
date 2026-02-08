"""Background daemon for config refresh."""
import logging
import threading
import time
import signal
import sys

logger = logging.getLogger(__name__)


class ConfigRefreshDaemon:
    """
    Background thread that refreshes config from blob storage.
    
    - Non-blocking
    - Validates before applying
    - Keeps retrying on failure
    """

    def __init__(self, blob_manager, local_config_path: str, refresh_interval: int):
        self.blob_manager = blob_manager
        self.local_config_path = local_config_path
        self.refresh_interval = refresh_interval
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        """Start the refresh daemon."""
        if self._thread and self._thread.is_alive():
            logger.warning("Daemon already running")
            return

        self._thread = threading.Thread(
            target=self._refresh_loop,
            daemon=True,
            name="ConfigRefreshDaemon"
        )
        self._thread.start()
        logger.info(f"✓ Config refresh daemon started (interval: {self.refresh_interval}s)")

    def stop(self):
        """Stop the daemon gracefully."""
        logger.info("Stopping config refresh daemon...")
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _refresh_loop(self):
        """Main refresh loop."""
        while not self._stop_event.is_set():
            try:
                success = self.blob_manager.fetch_config(self.local_config_path)
                
                if success:
                    if self.blob_manager.validate_config_file(self.local_config_path):
                        logger.info("✓ Config refreshed successfully")
                    else:
                        logger.error("Config validation failed - keeping old config")
                else:
                    logger.warning("Config fetch failed")

            except Exception as e:
                logger.error(f"Config refresh error: {e}")

            # Wait for next interval
            self._stop_event.wait(self.refresh_interval)


def initial_config_fetch(blob_manager, local_config_path: str, refresh_interval: int) -> bool:
    """
    Blocking initial config fetch.
    Retries forever until successful.
    """
    logger.info("=" * 60)
    logger.info("INITIAL CONFIG FETCH")
    logger.info("=" * 60)

    attempt = 0
    while True:
        attempt += 1
        logger.info(f"Attempt {attempt}: Fetching config...")

        success = blob_manager.fetch_config(local_config_path)

        if success and blob_manager.validate_config_file(local_config_path):
            logger.info("✓ Initial config fetch successful!")
            logger.info("=" * 60)
            return True
        
        logger.error(f"✗ Fetch failed, retrying in {refresh_interval}s...")
        time.sleep(refresh_interval)


def setup_signal_handlers(daemon: ConfigRefreshDaemon):
    """Setup graceful shutdown handlers."""
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}")
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
