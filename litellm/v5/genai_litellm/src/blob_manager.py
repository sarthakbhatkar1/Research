"""Azure Blob Storage manager for config.yaml fetching."""
import logging
import os
import time
import yaml

logger = logging.getLogger(__name__)


class BlobConfigManager:
    """
    Manages fetching config.yaml from Azure Blob Storage.
    
    Features:
    - MI or connection string auth
    - Atomic file updates (temp -> rename)
    - YAML validation
    """

    def __init__(self, config):
        self.config = config
        self.blob_service_client = None
        self.container_client = None
        self._initialize_blob_client()

    def _initialize_blob_client(self):
        """Initialize Azure Blob Storage client."""
        try:
            from azure.storage.blob import BlobServiceClient
            from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

            if self.config.auth_type == "MI":
                if self.config.mi_client_id:
                    logger.info(f"Blob: Using User-Assigned MI: {self.config.mi_client_id[:8]}...")
                    credential = ManagedIdentityCredential(client_id=self.config.mi_client_id)
                else:
                    logger.info("Blob: Using System-Assigned MI")
                    credential = DefaultAzureCredential()

                self.blob_service_client = BlobServiceClient(
                    account_url=self.config.account_url,
                    credential=credential
                )
            
            elif self.config.auth_type == "CONNECTION_STRING":
                logger.info("Blob: Using connection string authentication")
                self.blob_service_client = BlobServiceClient.from_connection_string(
                    self.config.connection_string
                )
            
            else:
                raise ValueError(f"Invalid BLOB_AUTH_TYPE: {self.config.auth_type}")

            self.container_client = self.blob_service_client.get_container_client(
                self.config.container
            )

            logger.info(f"✓ Blob storage initialized: {self.config.container}")

        except Exception as e:
            logger.error(f"Failed to initialize blob client: {e}")
            raise

    def fetch_config(self, local_path: str) -> bool:
        """
        Fetch config.yaml from blob and save locally.
        
        Uses atomic write (temp -> rename).
        """
        try:
            blob_client = self.container_client.get_blob_client(self.config.config_blob_name)
            
            logger.info(f"Fetching {self.config.config_blob_name}...")
            
            # Download to memory
            blob_data = blob_client.download_blob()
            config_content = blob_data.readall()
            
            # Validate YAML
            try:
                yaml.safe_load(config_content)
            except yaml.YAMLError as e:
                logger.error(f"Invalid YAML in blob: {e}")
                return False
            
            # Atomic write: temp -> rename
            temp_path = f"{local_path}.tmp"
            with open(temp_path, 'wb') as f:
                f.write(config_content)
            
            os.replace(temp_path, local_path)
            
            logger.info(f"✓ Config saved to {local_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to fetch config: {e}")
            return False

    def validate_config_file(self, path: str) -> bool:
        """Validate config file exists and has valid structure."""
        try:
            if not os.path.exists(path):
                logger.warning(f"Config file not found: {path}")
                return False
            
            with open(path, 'r') as f:
                config_data = yaml.safe_load(f)
            
            if not isinstance(config_data, dict):
                logger.error("Config is not a dictionary")
                return False
            
            if "model_list" not in config_data:
                logger.error("Config missing 'model_list'")
                return False
            
            if not isinstance(config_data["model_list"], list):
                logger.error("'model_list' must be a list")
                return False
            
            logger.info(f"✓ Config valid: {len(config_data['model_list'])} models")
            return True
            
        except Exception as e:
            logger.error(f"Config validation error: {e}")
            return False
