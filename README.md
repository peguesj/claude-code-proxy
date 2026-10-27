# Anthropic API Proxy for Gemini & OpenAI Models 🔄

**Use Anthropic clients (like Claude Code) with Gemini, OpenAI, or direct Anthropic backends.** 🤝

A proxy server that lets you use Anthropic clients with Gemini, OpenAI, or Anthropic models themselves (a transparent proxy of sorts), all via LiteLLM. 🌉


![Anthropic API Proxy](pic.png)

## Quick Start ⚡

### Prerequisites

- OpenAI API key 🔑
- Google AI Studio (Gemini) API key (if using Google provider) 🔑
- Google Cloud Project with Vertex AI API enabled (if using Application Default Credentials for Gemini) ☁️
- [uv](https://github.com/astral-sh/uv) installed.

### Setup 🛠️

#### From source

1. **Clone this repository**:
   ```bash
   git clone https://github.com/1rgs/claude-code-proxy.git
   cd claude-code-proxy
   ```

2. **Install uv** (if you haven't already):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   *(`uv` will handle dependencies based on `pyproject.toml` when you run the server)*

3. **Configure Environment Variables**:
   Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and fill in your API keys and model configurations:

   *   `ANTHROPIC_API_KEY`: (Optional) Needed only if proxying *to* Anthropic models. If not provided, the proxy can fall back to other configured providers.
   *   `OPENAI_API_KEY`: Your OpenAI API key (Required if using the default OpenAI preference or as fallback).
   *   `GEMINI_API_KEY`: Your Google AI Studio (Gemini) API key (Required if `PREFERRED_PROVIDER=google` and `USE_VERTEX_AUTH=true`).
   *   `AZURE_API_KEY` / `AZURE_API_BASE` (Optional): If `ANTHROPIC_API_KEY` is not set and `AZURE_API_KEY` is configured, the proxy will automatically fall back to Azure OpenAI for Anthropic-like requests (e.g., `claude-3-sonnet-...` -> mapped to an Azure deployment).
   *   `USE_VERTEX_AUTH` (Optional): Set to `true` to use Application Default Credentials (ADC) will be used (no static API key required). Note: when USE_VERTEX_AUTH=true, you must configure `VERTEX_PROJECT` and `VERTEX_LOCATION`.
   *   `VERTEX_PROJECT` (Optional): Your Google Cloud Project ID (Required if `PREFERRED_PROVIDER=google` and `USE_VERTEX_AUTH=true`).
   *   `VERTEX_LOCATION` (Optional): The Google Cloud region for Vertex AI (e.g., `us-central1`) (Required if `PREFERRED_PROVIDER=google` and `USE_VERTEX_AUTH=true`).
   *   `PREFERRED_PROVIDER` (Optional): Set to `openai` (default), `google`, or `anthropic`. This determines the primary backend for mapping `haiku`/`sonnet`.
   *   `BIG_MODEL` (Optional): The model to map `sonnet` requests to. Defaults to `gpt-4.1` (if `PREFERRED_PROVIDER=openai`) or `gemini-2.5-pro-preview-03-25`. Ignored when `PREFERRED_PROVIDER=anthropic`.
   *   `SMALL_MODEL` (Optional): The model to map `haiku` requests to. Defaults to `gpt-4.1-mini` (if `PREFERRED_PROVIDER=openai`) or `gemini-2.0-flash`. Ignored when `PREFERRED_PROVIDER=anthropic`.

   **Mapping Logic:**
   - If `PREFERRED_PROVIDER=openai` (default), `haiku`/`sonnet` map to `SMALL_MODEL`/`BIG_MODEL` prefixed with `openai/`.
   - If `PREFERRED_PROVIDER=google`, `haiku`/`sonnet` map to `SMALL_MODEL`/`BIG_MODEL` prefixed with `gemini/` *if* those models are in the server's known `GEMINI_MODELS` list (otherwise falls back to OpenAI mapping).
   - If `PREFERRED_PROVIDER=anthropic`, `haiku`/`sonnet` requests are passed directly to Anthropic with the `anthropic/` prefix without remapping to different models.

4. **Run the server**:
   ```bash
   uv run uvicorn server:app --host 0.0.0.0 --port 8082 --reload
   ```
   *(`--reload` is optional, for development)*

#### Docker

If using docker, download the example environment file to `.env` and edit it as described above.
```bash
curl -O .env https://raw.githubusercontent.com/1rgs/claude-code-proxy/refs/heads/main/.env.example
```

Then, you can either start the container with [docker compose](https://docs.docker.com/compose/) (preferred):

```yml
services:
  proxy:
    image: ghcr.io/1rgs/claude-code-proxy:latest
    restart: unless-stopped
    env_file: .env
    ports:
      - 8082:8082
```

Or with a command:

```bash
docker run -d --env-file .env -p 8082:8082 ghcr.io/1rgs/claude-code-proxy:latest
```

### Using with Claude Code 🎮

1. **Install Claude Code** (if you haven't already):
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. **Connect to your proxy**:
   ```bash
   ANTHROPIC_BASE_URL=http://localhost:8082 claude
   ```

3. **That's it!** Your Claude Code client will now use the configured backend models (defaulting to Gemini) through the proxy. 🎯

### OpenAI-compatible clients (VS Code, Continue, Cline, etc.)

Use this base URL for clients that speak the OpenAI chat-completions protocol:

```text
http://127.0.0.1:8082/v1
```

The proxy exposes `POST /v1/chat/completions` (plus `/chat/completions` for clients
that append the path to a bare host) and `GET /v1/models`. Use `PROXY_API_KEY` as
the client API key when configured; otherwise a placeholder value is accepted.

```bash
curl http://127.0.0.1:8082/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-5.6-luna","messages":[{"role":"user","content":"Hello"}]}'
```

## Model Mapping 🗺️

The proxy automatically maps Claude models to either OpenAI or Gemini models based on the configured model:

| Claude Model | Default Mapping | When BIG_MODEL/SMALL_MODEL is a Gemini model |
|--------------|--------------|---------------------------|
| haiku | openai/gpt-4o-mini | gemini/[model-name] |
| sonnet | openai/gpt-4o | gemini/[model-name] |

### Supported Models

#### OpenAI Models
The following OpenAI models are supported with automatic `openai/` prefix handling:
- o3-mini
- o1
- o1-mini
- o1-pro
- gpt-4.5-preview
- gpt-4o
- gpt-4o-audio-preview
- chatgpt-4o-latest
- gpt-4o-mini
- gpt-4o-mini-audio-preview
- gpt-4.1
- gpt-4.1-mini

#### Gemini Models
The following Gemini models are supported with automatic `gemini/` prefix handling:
- gemini-2.5-pro
- gemini-2.5-flash

### Model Prefix Handling
The proxy automatically adds the appropriate prefix to model names:
- OpenAI models get the `openai/` prefix
- Gemini models get the `gemini/` prefix
- The BIG_MODEL and SMALL_MODEL will get the appropriate prefix based on whether they're in the OpenAI or Gemini model lists

For example:
- `gpt-4o` becomes `openai/gpt-4o`
- `gemini-2.5-pro-preview-03-25` becomes `gemini/gemini-2.5-pro-preview-03-25`
- When BIG_MODEL is set to a Gemini model, Claude Sonnet will map to `gemini/[model-name]`

### Customizing Model Mapping

Control the mapping using environment variables in your `.env` file or directly:

**Example 1: Default (Use OpenAI)**
No changes needed in `.env` beyond API keys, or ensure:
```dotenv
OPENAI_API_KEY="your-openai-key"
GEMINI_API_KEY="your-google-key" # Needed if PREFERRED_PROVIDER=google
# PREFERRED_PROVIDER="openai" # Optional, it's the default
# BIG_MODEL="gpt-4.1" # Optional, it's the default
# SMALL_MODEL="gpt-4.1-mini" # Optional, it's the default
```

**Example 2a: Prefer Google (using GEMINI_API_KEY)**
```dotenv
GEMINI_API_KEY="your-google-key"
OPENAI_API_KEY="your-openai-key" # Needed for fallback
PREFERRED_PROVIDER="google"
# BIG_MODEL="gemini-2.5-pro" # Optional, it's the default for Google pref
# SMALL_MODEL="gemini-2.5-flash" # Optional, it's the default for Google pref
```

**Example 2b: Prefer Google (using Vertex AI with Application Default Credentials)**
```dotenv
OPENAI_API_KEY="your-openai-key" # Needed for fallback
PREFERRED_PROVIDER="google"
VERTEX_PROJECT="your-gcp-project-id"
VERTEX_LOCATION="us-central1"
USE_VERTEX_AUTH=true
# BIG_MODEL="gemini-2.5-pro" # Optional, it's the default for Google pref
# SMALL_MODEL="gemini-2.5-flash" # Optional, it's the default for Google pref
```

**Example 3: Use Direct Anthropic ("Just an Anthropic Proxy" Mode)**
```dotenv
ANTHROPIC_API_KEY="sk-ant-..."
PREFERRED_PROVIDER="anthropic"
# BIG_MODEL and SMALL_MODEL are ignored in this mode
# haiku/sonnet requests are passed directly to Anthropic models
```

*Use case: This mode enables you to use the proxy infrastructure (for logging, middleware, request/response processing, etc.) while still using actual Anthropic models rather than being forced to remap to OpenAI or Gemini.*

**Example 4: Use Specific OpenAI Models**
```dotenv
OPENAI_API_KEY="your-openai-key"
GEMINI_API_KEY="your-google-key"
PREFERRED_PROVIDER="openai"
BIG_MODEL="gpt-4o" # Example specific model
SMALL_MODEL="gpt-4o-mini" # Example specific model
```

## How It Works 🧩

This proxy works by:

1. **Receiving requests** in Anthropic's API format 📥
2. **Translating** the requests to OpenAI format via LiteLLM 🔄
3. **Sending** the translated request to OpenAI 📤
4. **Converting** the response back to Anthropic format 🔄
5. **Returning** the formatted response to the client ✅

The proxy handles both streaming and non-streaming responses, maintaining compatibility with all Claude clients. 🌊

### Frontier and Opus 1M routing

On OpenAI, Azure OpenAI, and Azure AI Foundry, normal Opus requests use
`FRONTIER_MODEL` (default `gpt-6-astra`). Existing `BIG_MODEL` / Sonnet and
`SMALL_MODEL` / Haiku settings are preserved.

| Opus 1M effort | Backend model |
| --- | --- |
| `low` | `gpt-5.6-luna` |
| `medium` or `med` (default) | `gpt-5.6-terra` |
| `high` | `gpt-5.6-sol` |

Use `opus[1m]` or a versioned alias such as `claude-opus-4-6[1m]`, with
`output_config.effort`. Explicit `-low`, `-medium`, `-med`, and `-high` suffixes
also work; the request field takes precedence. Claude Code's `context-1m-*`
beta header is recognized for Opus requests. The alias selects a model tier;
it does not increase the underlying deployment's context window.

The proxy translates Anthropic's `output_config` into provider-compatible
reasoning and structured-output parameters. GPT-6 Astra uses LiteLLM's
Responses adapter, as do GPT-5.6 requests containing tools, preserving reasoning
effort and tool calls in both streaming and non-streaming mode. Foundry uses
the same account and credentials through its Azure OpenAI Responses endpoint.
Anthropic passthrough keeps its native `output_config` unchanged.

Regression checks (no provider credentials required):

```bash
.venv/bin/python tests/test_model_upgrade.py
```

## Management Tools

This fork ships a macOS management suite for running the proxy as a background service.

For a full explanation of the application architecture, role-based model routing,
terminal switcher, and menu bar interface, see
[`docs/APPLICATION_GUIDE.md`](docs/APPLICATION_GUIDE.md).

### CLI — `ccp-litellm`

Installed at `~/.local/bin/ccp-litellm`:

```bash
ccp-litellm start         # nohup uvicorn on :8082, logs to /tmp/ccp-server.log
ccp-litellm stop          # SIGTERM then SIGKILL
ccp-litellm restart
ccp-litellm status        # running state, PID, provider, model mapping
ccp-litellm log [-f]      # tail /tmp/ccp-server.log (503 model_not_deployed lines get a redeploy hint)
ccp-litellm troubleshoot  # port/dep/env/HTTP health diagnostics
```

Also invokable as the `/ccp-litellm` Claude Code slash command.

#### Model groups (on-demand Foundry deployments)

Expensive Azure AI Foundry deployments live in resource groups defined in
`ccp_groups.json`. Groups with `policy: on_demand` (billing `idle_cost`) are deployed
when needed and torn down after `idle_teardown_minutes` (default 60) without use. Pay-per-token
groups stay `always_on`. The `ccp` command is the user-facing entry point. The `ccp groups`
and `ccp active` subcommands delegate to `ccp-litellm`, which uses `ccp_groups_cli.py`.

```bash
ccp groups list [-v]                    # groups, status, deployed model counts, last use
ccp groups deploy <id> [--models a,b]   # deploy models (default: all) in a group
ccp groups teardown <id> [--models a,b] # tear down models (default: all)
ccp groups redeploy <id> [--models a,b] # redeploy models (default: all)
ccp groups pick <id> [--action deploy|teardown|redeploy]
                                        # interactive multi-select: type to search, Space toggles
                                        # (on a section header: whole section), Ctrl-A / Ctrl-N
                                        # select / clear all shown, Tab cycles the action, Enter confirms
ccp active                              # big / small / frontier models and recent usage
```

Lifecycle calls go to the proxy API (`CCP_URL`, default `http://localhost:8082`) at
`/ccp/groups/{id}/{deploy|teardown|redeploy}` and return immediately (202). Work runs in the background,
so check progress with `ccp groups list`. If the proxy is down or does not expose `/ccp/*` yet, `groups list`
and `active` fall back to the local registry and `.env` mapping with a warning. Lifecycle commands
require the server.

When a request targets a torn-down model, the proxy returns HTTP 503 `model_not_deployed` with a
`Retry-After` header. The CLI prints the model, group, and the exact redeploy command.
Redeploy with `ccp groups deploy <group> --models <model>` or from the CCP menubar.

### Menu Bar App — CCP LiteLLM.app

Build + install with:

```bash
bash build-menubar.sh
```

Provides:

- **Live status icon** — green/red/amber indicator, polls `http://localhost:8082/` every 5s
- **Start / Stop / Restart** — control the proxy from the menu bar
- **Profile submenu** — switch `CCP_PROFILE` between accounts/subscriptions/resources defined in `profiles.json`
- **Provider submenu** — switch `PREFERRED_PROVIDER` between azure / openai / google / anthropic
- **Presets submenu** — 8 curated BIG/SMALL/FRONTIER model tuples (Best Performance, Best Reasoning, Balanced, Cost Efficient, Speed, Coding, OpenAI Direct, 2026 Best Overall)
- **Settings window (⌘,)** — edit every `.env` variable with atomic patching that preserves comments + ordering
- **Model Manager (⌘M)** — browse live deployments/catalogs across Azure, OpenAI, Anthropic, and Google; deploy new Azure models with SKU + capacity; promote any result to BIG / SMALL / FRONTIER with one click
- **Login-item toggles** — manage `com.ccp.litellm.proxy` and `com.ccp.litellm.menubar` LaunchAgents
- **Troubleshoot sheet** — runs full diagnostics inline

### Model Switcher TUI

`./ccp-switcher` — interactive bash TUI for changing settings profiles and BIG/SMALL/FRONTIER model mappings.

### Settings Profiles

Copy `profiles.example.json` to `profiles.json` to manage multiple accounts, Azure subscriptions, Azure OpenAI resources, and Azure AI Foundry resources. Select the active profile with `CCP_PROFILE` in `.env`, the menu bar Profile submenu, or the TUI `O` action.

Secrets can stay in env vars with `api_key_env`, or in macOS Keychain with `api_key_keychain`.

### Azure Fallback

When `PREFERRED_PROVIDER=anthropic` and a request 404s on Anthropic's upstream, the proxy retries once against Azure OpenAI using the configured `AZURE_*` env vars. Controlled by a `_retried_with_azure` flag to prevent loops.

## Security: LiteLLM Supply Chain Policy

`pyproject.toml` pins `litellm>=1.77.7,<1.82.0`. Versions **1.82.7** and **1.82.8** are banned (malicious credential-stealing code, TeamPCP attack, March 2026). CVE-2024-6825 and CVE-2024-8984 are mitigated by the lower bound. See `.claude/CLAUDE.md` for the full policy and remediation checklist.

## Contributing 🤝

Contributions are welcome! Please feel free to submit a Pull Request. 🎁
