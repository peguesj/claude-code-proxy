# Claude Code Proxy Application Guide

Claude Code Proxy is a local Anthropic-compatible API server for Claude Code and other Anthropic clients. It accepts Anthropic `/v1/messages` requests, maps Claude model families onto configured backend models, sends the request through LiteLLM, then converts the response back into Anthropic-compatible shape.

## Purpose

The project creates practical parity between Claude Code's frontier-model expectations and models available through:

- Azure OpenAI
- Azure AI Foundry
- OpenAI direct
- Google Gemini / Vertex AI
- Anthropic passthrough

The key abstraction is role-based routing:

| Claude family | CCP role | Environment variable | Typical use |
| --- | --- | --- | --- |
| Haiku | `small` | `SMALL_MODEL` | fast edits, summaries, lightweight tool use |
| Sonnet | `big` | `BIG_MODEL` | general coding, planning, multi-step work |
| Opus | `frontier` | `FRONTIER_MODEL` | hardest reasoning and code tasks |

With `PREFERRED_PROVIDER=azure_ai`, for example, an Opus request can be routed to a Foundry deployment such as `azure_ai/gpt-5.2`.

## Server

Run the proxy:

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8082 --reload
```

Use it with Claude Code:

```bash
ANTHROPIC_BASE_URL=http://localhost:8082 ANTHROPIC_API_KEY=proxy claude
```

If `PROXY_API_KEY` is set, clients must send that value as `ANTHROPIC_API_KEY`, `x-api-key`, or a bearer token.

### Server Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Basic service discovery with links to status and model metadata. |
| `GET /ccp/status` | Runtime provider, route table, credential/config presence, Azure/OpenAI/Foundry context. |
| `GET /ccp/models` | Static and configured model catalog used for routing decisions. |
| `GET /ccp/profiles` | Sanitized settings profile, account, and provider instance inventory. |
| `GET /ccp/azure/discovery` | `az` CLI-backed Azure subscription, account, and optional deployment discovery. |
| `POST /v1/messages` | Anthropic Messages API compatibility endpoint. |
| `POST /v1/messages/count_tokens` | Anthropic token-count compatibility endpoint. |

## Configuration

The proxy reads `.env` at startup. Core routing values:

```dotenv
PREFERRED_PROVIDER=azure_ai
FRONTIER_MODEL=gpt-6-astra
BIG_MODEL=gpt-5.2-codex
SMALL_MODEL=gpt-5.2-chat
```

Normal Opus requests use `FRONTIER_MODEL`. Opus 1M aliases (or Opus requests
with a `context-1m-*` beta header) use GPT-5.6 Luna/Terra/Sol for low/medium/high
effort. `medium` and `med` are equivalent; unspecified effort defaults to medium.
Existing Sonnet and Haiku configuration is independent of this mapping.
GPT-6 Astra, and GPT-5.6 tool requests, use the Responses API through LiteLLM.
Anthropic `output_config` is translated rather than forwarded to those providers.

Azure OpenAI:

```dotenv
AZURE_API_KEY=...
AZURE_API_BASE=https://<resource>.openai.azure.com/
AZURE_API_VERSION=2025-04-01-preview
AZURE_DEPLOYMENT_MAP={"gpt-5.2":"gpt-5-2"}
```

Azure AI Foundry:

```dotenv
AZURE_AI_API_KEY=...
AZURE_AI_API_BASE=https://<project-or-account>.services.ai.azure.com/models
AZURE_AI_API_VERSION=2025-04-01-preview
AZURE_AI_RESOURCE_GROUP=<resource-group>
AZURE_AI_ACCOUNT_NAME=<ai-services-account>
AZURE_AI_LOCATION=eastus2
```

Provider values supported by the server and UI:

- `azure`
- `azure_ai`
- `openai`
- `google`
- `anthropic`

### Settings Profiles

Profiles let one local proxy switch between multiple authorizations, Azure subscriptions, and provider resource instances without flattening everything into one `.env`.

```dotenv
CCP_PROFILES_FILE=/Users/jeremiah/Developer/claude-code-proxy/profiles.json
CCP_PROFILE=azure-sponsorship
```

Start from `profiles.example.json`. A profile can contain both an Azure OpenAI instance and an Azure AI Foundry instance, each with its own subscription, resource group, account, endpoint, API version, and deployment map.

Secrets should stay out of JSON. Use an environment variable reference:

```json
{ "api_key_env": "AZURE_PROD_OPENAI_KEY" }
```

or a macOS Keychain reference:

```json
{
  "api_key_keychain": {
    "service": "ccp.azure_ai",
    "account": "foundry-eastus2"
  }
}
```

Store a Keychain secret with:

```bash
security add-generic-password -U -s ccp.azure_ai -a foundry-eastus2 -w '<api-key>'
```

The server resolves settings in this order: active profile provider instance, profile route/provider defaults, then `.env`.

For Azure profile setup, `GET /ccp/azure/discovery?include_deployments=true` returns the logged-in Azure account, subscriptions, Cognitive Services/OpenAI resources, and deployments without including API keys.

## Terminal Interface

Run the terminal switcher:

```bash
./ccp-switcher
```

Main actions:

| Key | Action |
| --- | --- |
| `O` | Select the active settings profile from `profiles.json`. |
| `P` | Open preset profiles. |
| `F` | Select frontier model, used for Opus requests. |
| `B` | Select big model, used for Sonnet requests. |
| `S` | Select small model, used for Haiku requests. |
| `V` | Select preferred provider. |
| `D` | Show diagnostics, including live `/ccp/status` routes if the proxy is running. |
| `R` | Restart the proxy. |
| `Q` | Quit. |
| `1`-`8` | Apply quick presets. |

The switcher writes `.env` and asks whether to restart the proxy after changes.

## Menu Bar UI

Build and install the macOS menu bar app:

```bash
bash build-menubar.sh
```

The menu bar app provides:

- Live running/stopped status for the proxy on port `8082`
- Start, stop, and restart controls
- Profile selector
- Provider selector
- Preset selector
- Settings window
- Model Manager
- Copy Client Setup
- Log viewer
- Terminal switcher launcher
- Troubleshooting report
- Launch-at-login toggles

### Settings Window

The settings window edits `.env` without manually opening the file. It includes:

- Preferred provider
- `CCP_PROFILE` and `CCP_PROFILES_FILE`
- `FRONTIER_MODEL`, `BIG_MODEL`, `SMALL_MODEL`
- Optional `PROXY_API_KEY`
- Azure OpenAI key/base/version
- Azure AI Foundry key/base/version/resource context
- Azure deployment resource context
- OpenAI, Anthropic, and Gemini keys
- Restart-on-save toggle

### Model Manager

The Model Manager has provider tabs:

- Azure OpenAI
- Azure AI Foundry
- OpenAI
- Anthropic
- Google

For each provider, the table shows model/deployment id, model name, version, SKU, capacity, and status. Selecting a row and choosing `Use as BIG`, `Use as SMALL`, or `Use as FRONTIER` updates `.env` and restarts the proxy.

Azure OpenAI also supports deployment creation from the Azure model catalog when the Azure resource context is configured.

## Operational Notes

- The proxy caps OpenAI, Azure, Azure AI, and Gemini `max_tokens` to `16384` to avoid backend limit errors.
- Tool result content is flattened for OpenAI-compatible backends when needed.
- Anthropic passthrough mode keeps Claude model names and sends to Anthropic directly.
- If Anthropic passthrough fails with a not-found style error and Azure credentials are configured, the server retries once against the configured Azure provider.
- Azure AI Foundry model availability and billing are subscription-dependent. Marketplace-backed models such as Claude can fail on sponsorship/credit subscriptions even when catalog and quota appear available.

## Debugging

Check the status endpoint:

```bash
curl -s http://localhost:8082/ccp/status | jq .
```

Check the route catalog:

```bash
curl -s http://localhost:8082/ccp/models | jq .
```

Use the menu bar app's `Troubleshoot...` item or run the service CLI if installed:

```bash
ccp-litellm troubleshoot
ccp-litellm log -f
```
