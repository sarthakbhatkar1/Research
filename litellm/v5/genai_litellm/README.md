# GenAI LiteLLM Service

Production-ready LLM inference service built on LiteLLM with full enterprise support.

## Features

✅ **Multiple Managed Identities** - Different MI per Azure OpenAI region  
✅ **Azure Blob Storage** - Config management with hot-reload  
✅ **Redis Caching** - Resilient with automatic in-memory fallback  
✅ **Databricks Support** - Service Principal authentication  
✅ **Load Balancing** - Multi-region failover  
✅ **Zero Downtime** - Atomic config updates  
✅ **Production Ready** - Comprehensive logging, error handling  

---

## Architecture

```
Container Start
├── Load environment config
├── Initialize Redis (with fallback)
├── Initialize Blob Storage client
├── Fetch config.yaml (blocks until success)
├── Start config refresh daemon (background)
└── Start LiteLLM server (owns ASGI lifecycle)

On LLM Request
├── LiteLLM receives request
├── Router selects deployment
├── LiteLLM fetches MI token (per client_id)
├── Token cached internally
└── Request forwarded to Azure OpenAI

Config Refresh (every N seconds)
├── Fetch latest config from Blob
├── Validate YAML
├── Atomic swap (temp → rename)
├── LiteLLM auto-reloads
└── On failure: log error, keep old config
```

---

## Quick Start

### 1. Prepare config.yaml

```yaml
model_list:
  # Multiple regions with different MIs
  - model_name: gpt-4
    litellm_params:
      model: azure/gpt-4-eastus
      api_base: https://eastus-openai.openai.azure.com/
      api_version: "2024-02-01"
      client_id: os.environ/EASTUS_MI_CLIENT_ID
      tenant_id: os.environ/AZURE_TENANT_ID
    litellm_settings:
      enable_azure_ad_token_refresh: true

  - model_name: gpt-4
    litellm_params:
      model: azure/gpt-4-westus
      api_base: https://westus-openai.openai.azure.com/
      api_version: "2024-02-01"
      client_id: os.environ/WESTUS_MI_CLIENT_ID
      tenant_id: os.environ/AZURE_TENANT_ID
    litellm_settings:
      enable_azure_ad_token_refresh: true

router_settings:
  routing_strategy: simple-shuffle
  num_retries: 2
  timeout: 600
```

### 2. Upload to Blob Storage

```bash
az storage blob upload \
  --account-name yourstorage \
  --container-name litellm-config \
  --name config.yaml \
  --file config.yaml \
  --auth-mode login
```

### 3. Set Environment Variables

```bash
# Blob Storage
BLOB_AUTH_TYPE=MI
BLOB_ACCOUNT_URL=https://yourstorage.blob.core.windows.net
BLOB_DOC_CONTAINER=litellm-config

# LiteLLM
LITELLM_YAML_REFRESH_INTERVAL=60

# Azure Identity
AZURE_TENANT_ID=your-tenant-id
EASTUS_MI_CLIENT_ID=eastus-mi-client-id
WESTUS_MI_CLIENT_ID=westus-mi-client-id
```

### 4. Deploy

```bash
# Build
docker build -t genai-litellm:latest .

# Run
docker run -p 8000:8000 \
  -e BLOB_AUTH_TYPE=MI \
  -e BLOB_ACCOUNT_URL=... \
  -e AZURE_TENANT_ID=... \
  -e EASTUS_MI_CLIENT_ID=... \
  -e WESTUS_MI_CLIENT_ID=... \
  genai-litellm:latest
```

---

## Configuration Guide

### Multiple Managed Identities

LiteLLM natively supports different MIs per deployment:

```yaml
# Each deployment uses its own MI
- model_name: gpt-4
  litellm_params:
    client_id: os.environ/EASTUS_MI_CLIENT_ID  # Different per region
    tenant_id: os.environ/AZURE_TENANT_ID
  litellm_settings:
    enable_azure_ad_token_refresh: true  # LiteLLM handles tokens
```

**Environment variables:**
```bash
EASTUS_MI_CLIENT_ID=11111111-1111-1111-1111-111111111111
WESTUS_MI_CLIENT_ID=22222222-2222-2222-2222-222222222222
```

**⚠️ CRITICAL:** Environment variable names in config.yaml MUST match exactly.

### Databricks

Single SPN for all Databricks models:

```yaml
- model_name: llama-3-70b
  litellm_params:
    model: databricks/databricks-meta-llama-3-1-70b-instruct
    api_base: https://workspace.databricks.com/serving-endpoints
    client_id: os.environ/DATABRICKS_CLIENT_ID
    client_secret: os.environ/DATABRICKS_CLIENT_SECRET
    tenant_id: os.environ/DATABRICKS_TENANT_ID
```

### Redis (Optional)

Redis is optional - service auto-falls back to in-memory cache:

```bash
REDIS_HOST=your-redis.redis.cache.windows.net
REDIS_AUTH_TYPE=MI
REDIS_MI_CLIENT_ID=your-infra-mi-client-id
```

---

## How It Works

### 1. Managed Identity Tokens

**LiteLLM handles MI tokens automatically - no custom code needed!**

- LiteLLM reads `client_id` from config.yaml
- Uses Azure `DefaultAzureCredential`
- Caches tokens internally
- Auto-refreshes before expiry

**You just configure:**
```yaml
litellm_settings:
  enable_azure_ad_token_refresh: true
```

### 2. Config Refresh

**Two-phase approach:**

**Startup (Blocking):**
- Retries forever until valid config fetched
- Service won't start without config

**Runtime (Non-blocking):**
- Background daemon fetches every N seconds
- Atomic file updates
- Invalid configs rejected
- Service keeps running with old config on failure

### 3. Redis Fallback

- Redis unavailable → automatic in-memory fallback
- Service never crashes
- Warning logged
- Transparent to application

### 4. Load Balancing

Same `model_name` = load balanced group:

```yaml
- model_name: gpt-4  # Same name
  # East US deployment

- model_name: gpt-4  # Same name = load balanced
  # West US deployment
```

LiteLLM handles routing, failover, retries.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| **Blob Storage** |
| BLOB_AUTH_TYPE | Yes | MI | MI or CONNECTION_STRING |
| BLOB_ACCOUNT_URL | Yes (MI) | - | Storage account URL |
| BLOB_DOC_CONTAINER | Yes | litellm-config | Container name |
| BLOB_MI_CLIENT_ID | No | - | User-assigned MI |
| **LiteLLM** |
| LITELLM_YAML_REFRESH_INTERVAL | Yes | 60 | Refresh interval (seconds) |
| LITELLM_YAML_STORAGE_PATH | No | config.yaml | Local config path |
| LITELLM_PORT | No | 8000 | Server port |
| **Azure Identity** |
| AZURE_TENANT_ID | Yes | - | Azure tenant ID |
| *_MI_CLIENT_ID | Per region | - | MI client IDs |
| **Redis** (Optional) |
| REDIS_HOST | No | - | Redis hostname |
| REDIS_AUTH_TYPE | If enabled | - | PASSWORD or MI |
| **Databricks** (Optional) |
| DATABRICKS_CLIENT_ID | If using | - | SPN client ID |
| DATABRICKS_CLIENT_SECRET | If using | - | SPN secret |
| DATABRICKS_TENANT_ID | If using | - | Tenant ID |

---

## API Usage

LiteLLM exposes OpenAI-compatible endpoints:

```bash
# Chat completions
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# List models
curl http://localhost:8000/v1/models

# Health check
curl http://localhost:8000/health
```

---

## Deployment to AKS

### Prerequisites

- User-Assigned MIs with appropriate roles
- Azure Storage Account with `litellm-config` container
- Workload Identity enabled on AKS

### Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: genai-litellm
spec:
  replicas: 2
  template:
    metadata:
      labels:
        azure.workload.identity/use: "true"
    spec:
      serviceAccountName: genai-litellm-sa
      containers:
      - name: litellm
        image: youracr.azurecr.io/genai-litellm:latest
        env:
        - name: BLOB_AUTH_TYPE
          value: "MI"
        - name: BLOB_ACCOUNT_URL
          value: "https://yourstorage.blob.core.windows.net"
        - name: BLOB_DOC_CONTAINER
          value: "litellm-config"
        - name: LITELLM_YAML_REFRESH_INTERVAL
          value: "60"
        - name: AZURE_TENANT_ID
          value: "your-tenant-id"
        - name: EASTUS_MI_CLIENT_ID
          value: "eastus-mi-client-id"
        - name: WESTUS_MI_CLIENT_ID
          value: "westus-mi-client-id"
```

---

## Troubleshooting

### Pod stuck in Init

**Cause:** Can't fetch config from blob  
**Fix:** Check blob permissions, verify BLOB_ACCOUNT_URL

### 401 Unauthorized to Azure OpenAI

**Cause:** MI lacks permissions  
**Fix:** Verify `Cognitive Services OpenAI User` role assigned

### Config not updating

**Cause:** Invalid YAML or blob not updated  
**Fix:** Check logs for validation errors, verify blob upload

### Redis connection failed

**Expected:** Service continues with in-memory fallback  
**If crashes:** Bug - should never happen

---

## Critical Awareness

### 1. LiteLLM Owns the Server
- LiteLLM starts its own ASGI server
- Cannot add custom FastAPI routes
- Extend via LiteLLM callbacks/plugins

### 2. MI Token Handling
- LiteLLM fetches and caches tokens
- No custom token provider needed
- Token fetch happens on FIRST request

### 3. Environment Variable Matching
```yaml
# config.yaml
client_id: os.environ/EASTUS_MI_CLIENT_ID

# Kubernetes - MUST MATCH EXACTLY
- name: EASTUS_MI_CLIENT_ID
```

### 4. Config Refresh Behavior
- **Startup:** BLOCKS until success
- **Runtime:** Non-blocking, retries on failure
- **Invalid config:** Rejected, keeps old config

### 5. Redis is Optional
- NOT used for MI tokens
- Automatic fallback to in-memory
- Service never crashes due to Redis

---

## Project Structure

```
genai_litellm/
├── src/
│   ├── main.py              # Entrypoint
│   ├── env_config.py        # Environment parser
│   ├── blob_manager.py      # Blob Storage client
│   ├── config_daemon.py     # Config refresh daemon
│   └── redis_client.py      # Redis with fallback
├── Dockerfile
├── requirements.txt
├── config.yaml.example
└── .env.example
```

---

## License

[Your License]
