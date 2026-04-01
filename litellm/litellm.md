# LiteLLM as an AI Gateway: What We Have and What We Are Not Using

**Prepared by:** AI Platform Engineering
**Date:** April 2026
**Audience:** Managing Director

---

## Context

We run LiteLLM as our internal AI gateway for LLM serving. Active workstreams include chat completions, embeddings, images, audio, video, responses, and batch APIs.

This document maps out the broader surface area of LiteLLM that is available to us but not yet activated, across governance, cost control, reliability, and agent infrastructure, along with the business value each capability unlocks.

---

## 1. API Endpoints: Active vs. Available

Every endpoint below exists within our current LiteLLM deployment. No new infrastructure is required to activate the ones not yet started.

| Endpoint | Purpose | Status |
|---|---|---|
| /chat/completions | Core LLM inference across all text models | Live |
| /completions | Legacy text completion format | Live |
| /embeddings | Vector embeddings for RAG, search, and similarity | WIP |
| /images/generations | Image generation (DALL-E, Stable Diffusion, etc.) | WIP |
| /audio/speech and /audio/transcriptions | TTS and speech-to-text (Whisper, Azure Speech) | WIP |
| /videos | Video generation (Sora, RunwayML Gen-4) | WIP |
| /batches | Async bulk inference at reduced cost | WIP |
| /responses | OpenAI Responses API with built-in tool use | WIP |
| /rerank | Re-rank retrieval results to improve RAG quality | Not Started |
| /v1/search | Unified web search across Exa AI, Perplexity, Tavily | Not Started |
| /a2a (Agent-to-Agent) | Agent gateway to invoke AI agents through one endpoint | Not Started |
| /messages | Native Anthropic Claude API passthrough | Not Started |

---

## 2. Governance and Financial Controls

This is where LiteLLM delivers the most immediate business value we are not yet capturing. LLM spend across projects is currently untracked at the individual team or project level.

**Virtual API Keys per Team / Project**
Issue isolated keys to every team with their own budget ceiling, rate limit (tokens per minute / requests per minute), and model allowlist. No team can see or exceed another's allocation. Keys can be revoked instantly without rotating underlying provider credentials.

**Real-time Spend Tracking**
Per-key and per-team token consumption and dollar cost is logged on every request. The admin dashboard provides daily and monthly breakdowns by model, provider, and team, all exportable as CSV.

**Hard Budget Limits and Auto-cutoff**
Set monthly budgets per project or team. When a budget ceiling is hit, the key is automatically blocked until the next budget cycle. No manual intervention required.

**Budget Alerts Before Breach**
Configurable spend alert thresholds (e.g. at 80% of budget) trigger notifications before any team goes over limit. Prevents end-of-month surprises on provider invoices.

**Request Tagging for Internal Chargeback**
Tag every request with project codes, use-case identifiers, or cost centres. Enables internal chargeback reports without any changes to the calling applications.

**SSO / SAML and Audit Logs**
Enterprise auth (SSO/SAML) for the admin dashboard. Full audit log of who created or revoked keys, changed budgets, and accessed models. Required for regulatory compliance.

---

## 3. Safety and Compliance: Guardrails

LiteLLM supports configurable guardrails that run before and after every model call. These complement our existing RAI platform and can be configured per team or per key.

**Prompt Injection Detection**
Blocks jailbreak and injection attempts via Presidio or custom regex middleware before the request reaches the model.

**PII and Secret Redaction**
Strips names, email addresses, API keys, or regulated identifiers from prompts before sending to external providers. Configurable per key or per team.

**Per-Key Guardrail Toggle**
Teams running internal tooling that does not require strict guardrails can have them disabled at the key level without affecting other teams.

**Guardrails API v2 (Streaming)**
Released in late 2025. Guardrails now apply to streaming responses and tool call outputs, not just static completions.

**On-prem Routing for Sensitive Data**
Route PII-sensitive or regulated workloads automatically to self-hosted models (Ollama / vLLM), while standard queries hit cloud providers. Policy is enforced at the gateway and applications require no changes.

**Guardrail Playground (UI)**
Test guardrail rules interactively through the admin UI before applying them to production keys. Reduces configuration risk significantly.

---

## 4. Reliability and Routing

These gateway-level reliability features are available in our deployment but largely unconfigured across most use cases.

**Multi-deployment Load Balancing**
Distribute traffic across multiple Azure OpenAI deployments, regions, or cross-provider. Prevents single-deployment TPM throttling at scale.

**Automatic Retry and Fallback**
On provider timeout or rate-limit error, LiteLLM automatically retries or falls back to a secondary model with no change required in the calling application.

**Redis Response Caching**
Identical prompts return cached responses at zero inference cost and sub-millisecond latency. Cache can be namespaced by team or user.

**Semantic Caching**
Goes beyond exact-match caching. Semantically similar prompts return cached answers. High value for customer support, FAQ, and documentation use cases where phrasing varies but intent is the same.

**Dynamic Rate Limits per Team**
Teams with priority SLA can have reserved capacity. Dynamic rate limiting ensures high-priority workloads are not starved during burst periods from other projects.

**Tag-based Model Routing**
Route requests to specific models or deployments based on request tags. For example, route all requests tagged "sensitive" automatically to a private on-prem model.

---

## 5. Observability

LiteLLM supports native callbacks to all major observability platforms. Any platform already in use by our engineering teams can be connected without changing application code.

**Custom Logger Hooks**
Pre-call, post-call, and error hooks allow injecting custom logic such as writing to an internal audit store, redacting fields, or enriching metadata, without modifying LiteLLM itself.

**Native Observability Callbacks**
Out-of-the-box callbacks to Langfuse, MLflow, Lunary, Datadog, Prometheus, and Grafana. Captures tokens, latency, cost, and errors per request automatically.

**Endpoint Activity Metrics (UI)**
The admin dashboard shows per-endpoint traffic, latency, and error rates in real time. No external tooling required for basic platform health visibility.

**End-User Spend Tracking**
Track LLM spend down to the individual end-user level. Relevant for internal tools where individual accountability matters.

---

## 6. Emerging Capabilities (Released 2025)

These are newer LiteLLM features representing the frontier of what the platform can support. Each is available in the current codebase and requires only configuration to activate.

**Agent Gateway (A2A: Agent-to-Agent)**
Register AI agents (LangGraph, Azure AI Foundry, Bedrock AgentCore) behind LiteLLM. Any calling system invokes them through a single authenticated endpoint with full request logging and access controls. Released Q4 2025.

**MCP Server Integration (Model Context Protocol)**
Expose tools such as databases, filesystems, and internal APIs to LLMs via the chat completions endpoint. Centralises tool access control across all AI applications through the gateway.

**MCP Hub**
Publish and discover MCP servers within the organisation through a shared registry. Teams can find and reuse tool integrations without duplicating setup.

**Unified Search API (/v1/search)**
Single endpoint across 6 search providers including Exa AI, Perplexity, and Tavily. Includes cost tracking and fallback routing. Useful for grounding agents with live external data.

**RAG Query Endpoint**
Native retrieval-augmented generation endpoint with vector store integrations (Milvus, Azure AI Search). Moves retrieval infrastructure inside the gateway layer rather than each application managing it independently.

**Rerank API (/rerank)**
Re-ranks retrieved documents for relevance before passing to the LLM. Materially improves RAG answer quality with minimal latency overhead. Cohere and other reranker models are supported.

**Cost Estimator (UI)**
Admin UI tool for estimating cost across models and request volumes before committing. Helps teams plan model selection and budget requests without running test traffic.

---

## 7. Provider Coverage

All providers below are accessible through our existing LiteLLM deployment with configuration only and no new infrastructure.

| Provider | Type | Notes |
|---|---|---|
| Azure OpenAI | Cloud LLM | Currently active |
| Databricks | Cloud LLM | Currently active |
| OpenAI | Cloud LLM | Ready to onboard |
| Anthropic Claude | Cloud LLM | Ready to onboard |
| Google Gemini | Cloud LLM | Ready to onboard |
| AWS Bedrock | Cloud LLM | Ready to onboard |
| Vertex AI | Cloud LLM | Ready to onboard |
| Cohere | Cloud LLM + Rerank | Ready to onboard |
| Mistral AI | Cloud LLM | Ready to onboard |
| Fireworks AI | Cloud LLM | Ready to onboard |
| Ollama | On-prem / Self-hosted | For sensitive and private workloads |
| vLLM | On-prem / Self-hosted | For sensitive and private workloads |
| NVIDIA NIM | On-prem / Self-hosted | GPU-accelerated inference |
| AWS Polly | TTS | Audio workloads |
| Azure AI Speech | TTS | Audio workloads |
| RunwayML Gen-4 | Video Generation | Video workloads |

Any provider onboarded will immediately inherit the existing virtual key infrastructure, spend tracking, and guardrails.

---

## Summary and Recommended Activation Order

Our LiteLLM deployment is currently being used as a model routing proxy, which is its narrowest function. The same platform, with configuration effort only and no new infrastructure, can deliver project-level financial controls and budget enforcement, compliance-grade guardrails and PII redaction, full audit logging for regulatory purposes, agent infrastructure for our growing AI agent workload, and a significant reduction in redundant LLM spend through caching and fallback routing.

Recommended activation priority:

1. **Virtual keys and team budgets** — immediate cost visibility and financial control per project
2. **Guardrails and audit logging** — compliance and governance baseline
3. **Caching and load balancing** — reliability and spend reduction
4. **Agent gateway** — as the agentic workload matures

---

*AI Platform Engineering · Internal Documentation · April 2026 · Confidential*
