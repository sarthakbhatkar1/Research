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
    def __init__(self, active_path: Path, last_good_path: Path):
        self.active_path = active_path
        self.last_good_path = last_good_path
        self.last_hash: Optional[str] = None

        self.storage_type = os.getenv("LITELLM_YAML_STORAGE_TYPE", "blob")
        self.storage_path = os.getenv("LITELLM_YAML_STORAGE_PATH")

        self.auth_type = os.getenv("BLOB_AUTH_TYPE", "CONNECTION_STRING")
        self.account_url = os.getenv("BLOB_ACCOUNT_URL")
        self.connection_string = os.getenv("BLOB_CONNECTION_STRING")
        self.container = os.getenv("BLOB_DOC_CONTAINER")

        self.credential = None
        self.blob_client = None

    async def _init_client(self):
        if self.blob_client:
            return

        if self.auth_type == "CONNECTION_STRING":
            client = BlobServiceClient.from_connection_string(
                self.connection_string
            )
        else:
            self.credential = ManagedIdentityCredential(
                client_id=os.getenv("BLOB_MI_CLIENT_ID")
            )
            client = BlobServiceClient(
                account_url=self.account_url,
                credential=self.credential,
            )

        self.blob_client = client.get_container_client(self.container)

    async def sync_from_blob(self) -> bool:
        """
        Returns True if config changed
        """
        await self._init_client()

        blob = self.blob_client.get_blob_client(self.storage_path)
        content = await (await blob.download_blob()).readall()

        new_hash = hashlib.sha256(content).hexdigest()

        if new_hash == self.last_hash:
            return False

        logger.info("Blob config changed, validating")

        data = yaml.safe_load(content)
        self._validate(data)

        tmp = self.active_path.with_suffix(".tmp")
        tmp.write_bytes(content)

        if self.active_path.exists():
            shutil.copy2(self.active_path, self.last_good_path)

        shutil.move(tmp, self.active_path)

        self.last_hash = new_hash
        logger.info("Config applied successfully")

        return True

    def _validate(self, cfg: dict):
        if "model_list" not in cfg or not cfg["model_list"]:
            raise ValueError("model_list missing or empty")
