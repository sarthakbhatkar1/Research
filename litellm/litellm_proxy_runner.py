#!/usr/bin/env python3
"""
Production-ready LiteLLM Proxy Runner
Properly uses LiteLLM's native proxy server with all features
- Hot config reload from Azure Blob Storage  
- Managed Identity token fetching via custom callback
- All LiteLLM proxy features (admin UI, keys, spend tracking, etc.)
- Safe for Gunicorn + standalone execution

APPROACH:
- Use LiteLLM's built-in proxy server (gets all features)
- Register custom callback for Managed Identity token fetching
- Hot reload updates config file that LiteLLM reads
- LiteLLM handles all routing, admin UI, etc.
"""

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
from fastapi import Request
from litellm.proxy.proxy_server import app, initialize
from litellm.integrations.custom_logger import CustomLogger
import litellm

from azure_token_wrapper import AzureTokenManager

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    """Global configuration singleton"""
    
    # Azure Blob Storage
    STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME")
    CONTAINER_NAME = os.getenv("AZURE_BLOB_CONTAINER_NAME", "litellm-config")
    BLOB_NAME = os.getenv("AZURE_BLOB_NAME", "proxy_config.yaml")
    
    # Local file paths
    CONFIG_DIR = Path("/app/config")
    ACTIVE_CONFIG_PATH = CONFIG_DIR / "proxy_config.yaml"  # LiteLLM will read this
    LAST_GOOD_CONFIG_PATH = CONFIG_DIR / "last_good_config.yaml"
    TEMP_CONFIG_PATH = CONFIG_DIR / "temp_config.yaml"
    
    # Reload interval
    RELOAD_INTERVAL_SECONDS = int(os.getenv("CONFIG_RELOAD_INTERVAL", "60"))
    
    @classmethod
    def validate(cls):
        """Validate required environment variables"""
        if not cls.STORAGE_ACCOUNT_NAME:
            raise ValueError("AZURE_STORAGE_ACCOUNT_NAME is required")
        
        # Create config directory
        cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================================
# MANAGED IDENTITY TOKEN PROVIDER FOR LITELLM
# ============================================================================

class ManagedIdentityTokenProvider(CustomLogger):
    """
    Custom LiteLLM callback that provides Managed Identity tokens
    
    LiteLLM calls this before making Azure OpenAI requests
    We intercept and inject the MI token
    """
    
    def __init__(self, token_manager: AzureTokenManager):
        self.token_manager = token_manager
        super().__init__()
    
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """
        Called before LiteLLM makes an API call
        
        We inject the Managed Identity token here
        """
        try:
            # Check if this is an Azure OpenAI call
            model = data.get("model", "")
            litellm_params = data.get("litellm_params", {})
            
            if model.startswith("azure/") or "azure" in litellm_params.get("custom_llm_provider", ""):
                # Check if config has azure_mi_client_id
                mi_client_id = litellm_params.get("azure_mi_client_id")
                
                if mi_client_id:
                    # Fetch token from MI
                    token = await self.token_manager.get_token(mi_client_id)
                    
                    # Inject token into request
                    data["litellm_params"]["api_key"] = token
                    
                    logger.debug(f"Injected MI token for model {model}")
        
        except Exception as e:
            logger.error(f"Failed to inject MI token: {e}")
            # Don't block the request - let LiteLLM handle auth failure
        
        return data


# ============================================================================
# CONFIG VALIDATOR
# ============================================================================

class ConfigValidator:
    """Validates LiteLLM proxy configuration"""
    
    @staticmethod
    def validate(config_data: dict) -> tuple[bool, Optional[str]]:
        """Validate config structure and required fields"""
        try:
            if "model_list" not in config_data:
                return False, "Missing 'model_list' key"
            
            model_list = config_data["model_list"]
            
            if not isinstance(model_list, list):
                return False, "'model_list' must be a list"
            
            if len(model_list) == 0:
                return False, "'model_list' cannot be empty"
            
            for idx, model in enumerate(model_list):
                if not isinstance(model, dict):
                    return False, f"Model at index {idx} is not a dictionary"
                
                if "model_name" not in model:
                    return False, f"Model at index {idx} missing 'model_name'"
                
                if "litellm_params" not in model:
                    return False, f"Model at index {idx} missing 'litellm_params'"
                
                litellm_params = model["litellm_params"]
                if not isinstance(litellm_params, dict):
                    return False, f"Model at index {idx} 'litellm_params' must be a dict"
                
                if "model" not in litellm_params:
                    return False, f"Model at index {idx} missing 'litellm_params.model'"
            
            logger.info(f"Config validation passed: {len(model_list)} models defined")
            return True, None
            
        except Exception as e:
            return False, f"Validation exception: {str(e)}"


# ============================================================================
# BLOB CONFIG FETCHER
# ============================================================================

class BlobConfigFetcher:
    """Fetches and manages config from Azure Blob Storage"""
    
    def __init__(self):
        self.blob_url = f"https://{Config.STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
        self.credential = None
        self.blob_client = None
        self.last_etag: Optional[str] = None
        self.last_content_hash: Optional[str] = None
    
    async def initialize(self):
        """Initialize Azure clients with Managed Identity"""
        try:
            self.credential = ManagedIdentityCredential()
            service_client = BlobServiceClient(
                account_url=self.blob_url,
                credential=self.credential
            )
            container_client = service_client.get_container_client(Config.CONTAINER_NAME)
            self.blob_client = container_client.get_blob_client(Config.BLOB_NAME)
            logger.info("Blob storage client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize blob client: {e}")
            raise
    
    async def download_initial_config(self) -> Path:
        """Download config on first startup"""
        logger.info("Downloading initial config from blob storage...")
        
        try:
            download_stream = await self.blob_client.download_blob()
            content = await download_stream.readall()
            
            # Save to active config
            Config.ACTIVE_CONFIG_PATH.write_bytes(content)
            
            # Also save as last good config
            shutil.copy2(Config.ACTIVE_CONFIG_PATH, Config.LAST_GOOD_CONFIG_PATH)
            
            # Store metadata for change detection
            properties = await self.blob_client.get_blob_properties()
            self.last_etag = properties.etag
            self.last_content_hash = hashlib.sha256(content).hexdigest()
            
            logger.info(f"Initial config downloaded: {len(content)} bytes")
            return Config.ACTIVE_CONFIG_PATH
            
        except Exception as e:
            logger.error(f"Failed to download initial config: {e}")
            raise
    
    async def check_for_updates(self) -> Optional[bytes]:
        """Check if blob has changed and return new content if so"""
        try:
            properties = await self.blob_client.get_blob_properties()
            current_etag = properties.etag
            
            if current_etag == self.last_etag:
                logger.debug("Config unchanged (etag match)")
                return None
            
            download_stream = await self.blob_client.download_blob()
            content = await download_stream.readall()
            content_hash = hashlib.sha256(content).hexdigest()
            
            if content_hash == self.last_content_hash:
                logger.debug("Config unchanged (content hash match)")
                self.last_etag = current_etag
                return None
            
            logger.info(f"Config changed detected: new etag={current_etag}")
            self.last_etag = current_etag
            self.last_content_hash = content_hash
            
            return content
            
        except Exception as e:
            logger.error(f"Failed to check for config updates: {e}")
            return None
    
    async def close(self):
        """Cleanup resources"""
        if self.credential:
            await self.credential.close()


# ============================================================================
# CONFIG MANAGER WITH HOT RELOAD
# ============================================================================

class ConfigManager:
    """Manages config lifecycle: fetch, validate, reload"""
    
    def __init__(self):
        self.fetcher = BlobConfigFetcher()
        self.reload_task: Optional[asyncio.Task] = None
        self.running = False
    
    async def initialize(self):
        """Initialize: fetch config and validate"""
        await self.fetcher.initialize()
        
        # Download initial config
        config_path = await self.fetcher.download_initial_config()
        
        # Load and validate
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
        
        is_valid, error = ConfigValidator.validate(config_data)
        
        if not is_valid:
            raise ValueError(f"Initial config validation failed: {error}")
        
        logger.info("Initial config ready")
    
    async def reload_config(self):
        """Check for config updates and reload if changed"""
        try:
            # Check for updates
            new_content = await self.fetcher.check_for_updates()
            
            if new_content is None:
                return  # No changes
            
            logger.info("Config change detected, starting reload process...")
            
            # Write to temp file
            Config.TEMP_CONFIG_PATH.write_bytes(new_content)
            
            # Load and validate
            try:
                with open(Config.TEMP_CONFIG_PATH, 'r') as f:
                    config_data = yaml.safe_load(f)
            except Exception as e:
                logger.error(f"Failed to parse new config YAML: {e}")
                Config.TEMP_CONFIG_PATH.unlink(missing_ok=True)
                return
            
            is_valid, error = ConfigValidator.validate(config_data)
            
            if not is_valid:
                logger.error(f"Config validation failed: {error}")
                Config.TEMP_CONFIG_PATH.unlink(missing_ok=True)
                return
            
            # Atomic swap
            try:
                # Backup current active config as last good
                shutil.copy2(Config.ACTIVE_CONFIG_PATH, Config.LAST_GOOD_CONFIG_PATH)
                
                # Replace active config with new one
                shutil.move(str(Config.TEMP_CONFIG_PATH), str(Config.ACTIVE_CONFIG_PATH))
                
                # Signal LiteLLM to reload
                # LiteLLM proxy has a reload endpoint we can call
                import httpx
                async with httpx.AsyncClient() as client:
                    try:
                        await client.post("http://localhost:8000/config/reload")
                        logger.info("✅ Config reloaded successfully")
                    except Exception as e:
                        logger.warning(f"Failed to trigger LiteLLM reload via API: {e}")
                        logger.info("Config file updated - LiteLLM will reload on next request")
                
            except Exception as e:
                logger.error(f"Failed to update config files: {e}")
                # Rollback
                if Config.LAST_GOOD_CONFIG_PATH.exists():
                    shutil.copy2(Config.LAST_GOOD_CONFIG_PATH, Config.ACTIVE_CONFIG_PATH)
                raise
            
        except Exception as e:
            logger.error(f"Config reload failed: {e}")
    
    async def reload_loop(self):
        """Background task that checks for config updates periodically"""
        self.running = True
        logger.info(f"Config reload loop started (interval={Config.RELOAD_INTERVAL_SECONDS}s)")
        
        while self.running:
            try:
                await asyncio.sleep(Config.RELOAD_INTERVAL_SECONDS)
                await self.reload_config()
            except Exception as e:
                logger.error(f"Error in reload loop: {e}")
    
    async def start_reload_loop(self):
        """Start the background reload task"""
        loop = asyncio.get_event_loop()
        self.reload_task = loop.create_task(self.reload_loop())
    
    async def stop_reload_loop(self):
        """Stop the background reload task"""
        self.running = False
        if self.reload_task:
            self.reload_task.cancel()
            try:
                await self.reload_task
            except asyncio.CancelledError:
                pass
    
    async def close(self):
        """Cleanup resources"""
        await self.stop_reload_loop()
        await self.fetcher.close()


# ============================================================================
# GLOBAL INSTANCES
# ============================================================================

token_manager: Optional[AzureTokenManager] = None
config_manager: Optional[ConfigManager] = None


# ============================================================================
# STARTUP/SHUTDOWN HOOKS
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    global token_manager, config_manager
    
    logger.info("Starting LiteLLM proxy with hot reload...")
    
    # Validate environment
    Config.validate()
    
    # Initialize token manager
    token_manager = AzureTokenManager()
    await token_manager.initialize()
    
    # Register MI token provider with LiteLLM
    mi_provider = ManagedIdentityTokenProvider(token_manager)
    litellm.callbacks = [mi_provider]
    logger.info("Registered Managed Identity token provider")
    
    # Initialize config manager
    config_manager = ConfigManager()
    await config_manager.initialize()
    
    # Initialize LiteLLM proxy with our config
    await initialize(
        config=str(Config.ACTIVE_CONFIG_PATH),
        telemetry=False
    )
    
    # Start reload loop
    await config_manager.start_reload_loop()
    
    logger.info("✅ LiteLLM proxy ready with hot reload")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down...")
    
    if config_manager:
        await config_manager.close()
    
    if token_manager:
        await token_manager.close()


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Main entry point for standalone execution"""
    import uvicorn
    
    logger.info("Starting in standalone mode...")
    
    # Run with uvicorn
    uvicorn.run(
        "litellm_proxy_runner_final:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info",
        reload=False
    )


if __name__ == "__main__":
    main()
