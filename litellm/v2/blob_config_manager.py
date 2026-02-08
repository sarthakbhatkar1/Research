# blob_config_manager.py

import asyncio
import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import yaml
from azure.identity.aio import ManagedIdentityCredential
from azure.storage.blob.aio import BlobServiceClient

logger = logging.getLogger("blob-config")


class BlobConfigManager:
    def __init__(
        self,
        account_name: str,
        container: str,
        blob_name: str,
        local_config_path: Path,
        poll_interval: int = 60,
    ):
        self.account_name = account_name
        self.container = container
        self.blob_name = blob_name
        self.local_config_path = local_config_path
        self.poll_interval = poll_interval

        self._credential: Optional[ManagedIdentityCredential] = None
        self._blob_client = None
        self._last_hash: Optional[str] = None
        self._running = False

    async def initialize(self):
        logger.info("[CONFIG] Initializing blob client")

        self._credential = ManagedIdentityCredential()
        service = BlobServiceClient(
            account_url=f"https://{self.account_name}.blob.core.windows.net",
            credential=self._credential,
        )

        container_client = service.get_container_client(self.container)
        self._blob_client = container_client.get_blob_client(self.blob_name)

        await self._download_and_activate(initial=True)

    async def _download_and_activate(self, initial: bool = False):
        logger.info("[CONFIG] Downloading config from blob")

        stream = await self._blob_client.download_blob()
        content = await stream.readall()

        content_hash = hashlib.sha256(content).hexdigest()
        if not initial and content_hash == self._last_hash:
            logger.debug("[CONFIG] No config change detected")
            return False

        tmp_path = self.local_config_path.with_suffix(".tmp")

        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(content)

        # Validate YAML
        try:
            data = yaml.safe_load(content)
            if not data or "model_list" not in data:
                raise ValueError("Invalid config: missing model_list")
        except Exception as e:
            logger.error(f"[CONFIG] Validation failed: {e}")
            tmp_path.unlink(missing_ok=True)
            return False

        # Backup old config
        if self.local_config_path.exists():
            shutil.copy2(
                self.local_config_path,
                self.local_config_path.with_suffix(".bak"),
            )

        # Atomic swap
        os.replace(tmp_path, self.local_config_path)

        self._last_hash = content_hash
        logger.info("[CONFIG] Config updated successfully")

        return True

    async def poll_loop(self, on_change_callback):
        logger.info(f"[CONFIG] Poll loop started (interval={self.poll_interval}s)")
        self._running = True

        while self._running:
            try:
                changed = await self._download_and_activate()
                if changed:
                    await on_change_callback()
            except Exception as e:
                logger.error(f"[CONFIG] Poll error: {e}")

            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self._running = False
        if self._credential:
            await self._credential.close()
