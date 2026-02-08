"""
Azure Blob Storage manager for config.yaml fetching and refresh.
Handles both Managed Identity and connection string authentication.
"""
import logging
import os
import tempfile
import time
import yaml
from typing import Optional

logger = logging.getLogger(__name__)


class BlobConfigManager:
    """
    Manages fetching and refreshing config.yaml from Azure Blob Storage.
    
    Features:
    - Resilient initial fetch with retries
    - Atomic file updates (temp file -> rename)
    - YAML validation before applying
    - Keeps last known good config on failure
    """

    def __init__(self, config):
        """
        Initialize blob config manager.
        
        Args:
            config: BlobConfig from env_config.py
        """
        self.config = config
        self.blob_service_client = None
        self.container_client = None
        self._initialize_blob_client()

    def _initialize_blob_client(self):
        """Initialize Azure Blob Storage client based on auth type."""
        try:
            from azure.storage.blob import BlobServiceClient
            from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

            if self.config.auth_type == "MI":
                # Use Managed Identity
                if self.config.mi_client_id:
                    logger.info(f"Using User-Assigned MI: {self.config.mi_client_id[:8]}...")
                    credential = ManagedIdentityCredential(client_id=self.config.mi_client_id)
                else:
                    logger.info("Using System-Assigned MI or DefaultAzureCredential")
                    credential = DefaultAzureCredential()

                self.blob_service_client = BlobServiceClient(
                    account_url=self.config.account_url,
                    credential=credential
                )
            
            elif self.config.auth_type == "CONNECTION_STRING":
                logger.info("Using connection string authentication")
                self.blob_service_client = BlobServiceClient.from_connection_string(
                    self.config.connection_string
                )
            
            else:
                raise ValueError(f"Invalid BLOB_AUTH_TYPE: {self.config.auth_type}")

            # Get container client
            self.container_client = self.blob_service_client.get_container_client(
                self.config.container
            )

            logger.info(f"✓ Blob storage client initialized: {self.config.container}")

        except ImportError as e:
            logger.error(f"Azure SDK not installed: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize blob client: {e}")
            raise

    def fetch_config(self, local_path: str) -> bool:
        """
        Fetch config.yaml from blob storage and save locally.
        
        Uses atomic write (temp file -> rename) to prevent partial updates.
        
        Args:
            local_path: Path where config.yaml should be saved
            
        Returns:
            True if successful, False otherwise
        """
        try:
            blob_client = self.container_client.get_blob_client(self.config.config_blob_name)
            
            logger.info(f"Fetching {self.config.config_blob_name} from blob storage...")
            
            # Download to memory first
            blob_data = blob_client.download_blob()
            config_content = blob_data.readall()
            
            # Validate YAML syntax before writing
            try:
                yaml.safe_load(config_content)
            except yaml.YAMLError as e:
                logger.error(f"Invalid YAML in blob config: {e}")
                return False
            
            # Atomic write: temp file -> rename
            temp_path = f"{local_path}.tmp"
            with open(temp_path, 'wb') as f:
                f.write(config_content)
            
            # Atomic rename (POSIX guarantees this is atomic)
            os.replace(temp_path, local_path)
            
            logger.info(f"✓ Config successfully fetched and saved to {local_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to fetch config from blob: {e}")
            return False

    def validate_config_file(self, path: str) -> bool:
        """
        Validate that a config file exists and is valid YAML.
        
        Args:
            path: Path to config file
            
        Returns:
            True if valid, False otherwise
        """
        try:
            if not os.path.exists(path):
                logger.warning(f"Config file not found: {path}")
                return False
            
            with open(path, 'r') as f:
                config_data = yaml.safe_load(f)
            
            # Basic validation: ensure it's a dict with model_list
            if not isinstance(config_data, dict):
                logger.error("Config is not a valid YAML dictionary")
                return False
            
            if "model_list" not in config_data:
                logger.error("Config missing required 'model_list' key")
                return False
            
            if not isinstance(config_data["model_list"], list):
                logger.error("'model_list' must be a list")
                return False
            
            logger.info(f"✓ Config validated: {len(config_data['model_list'])} models defined")
            return True
            
        except yaml.YAMLError as e:
            logger.error(f"YAML validation failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Config validation error: {e}")
            return False

    def refresh_config_with_retry(self, local_path: str, max_retries: int = 3, retry_delay: int = 5) -> bool:
        """
        Fetch config with retry logic.
        
        Args:
            local_path: Path where config should be saved
            max_retries: Maximum number of retry attempts
            retry_delay: Delay between retries in seconds
            
        Returns:
            True if successful, False otherwise
        """
        for attempt in range(1, max_retries + 1):
            if self.fetch_config(local_path):
                return True
            
            if attempt < max_retries:
                logger.warning(f"Retry {attempt}/{max_retries} in {retry_delay}s...")
                time.sleep(retry_delay)
        
        logger.error(f"Failed to fetch config after {max_retries} attempts")
        return False
