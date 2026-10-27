from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
import uvicorn
import logging
import json
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional, Union, Literal
import httpx
import os
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import litellm
import uuid
import time
from dotenv import load_dotenv
import re
import hashlib
from datetime import datetime
import sys
from profiles import (
    deployment_map,
    load_profiles,
    prepare_profile,
    profile_value,
    provider_setting,
    public_profiles,
    route_value,
)
from azure_discovery import AzureDiscoveryError, discovery as azure_discovery
import ccp_lifecycle

# Load environment variables from .env file
load_dotenv()
PROFILE_CONFIG = load_profiles()
PREPARED_PROFILE = prepare_profile(PROFILE_CONFIG, os.environ)

# Configure logging
logging.basicConfig(
    level=logging.WARN,  # Change to INFO level to show more details
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# Configure uvicorn to be quieter
import uvicorn
# Tell uvicorn's loggers to be quiet
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

# Create a filter to block any log messages containing specific strings
class MessageFilter(logging.Filter):
    def filter(self, record):
        # Block messages containing these strings
        blocked_phrases = [
            "LiteLLM completion()",
            "HTTP Request:", 
            "selected model name for cost calculation",
            "utils.py",
            "cost_calculator"
        ]
        
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            for phrase in blocked_phrases:
                if phrase in record.msg:
                    return False
        return True

# Apply the filter to the root logger to catch all messages
root_logger = logging.getLogger()
root_logger.addFilter(MessageFilter())

# Custom formatter for model mapping logs
class ColorizedFormatter(logging.Formatter):
    """Custom formatter to highlight model mappings"""
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    
    def format(self, record):
        if record.levelno == logging.debug and "MODEL MAPPING" in record.msg:
            # Apply colors and formatting to model mapping logs
            return f"{self.BOLD}{self.GREEN}{record.msg}{self.RESET}"
        return super().format(record)

# Apply custom formatter to console handler
for handler in logger.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.setFormatter(ColorizedFormatter('%(asctime)s - %(levelname)s - %(message)s'))

app = FastAPI()


@app.exception_handler(RequestValidationError)
async def log_request_validation_error(request: Request, exc: RequestValidationError):
    """Log schema failures without copying prompt or tool contents into logs."""
    diagnostics = [
        {
            "loc": list(error.get("loc", ())),
            "type": error.get("type"),
            "msg": error.get("msg"),
        }
        for error in exc.errors()
    ]
    unknown_block_shapes = []
    known_block_types = {"text", "image", "tool_use", "tool_result", "thinking", "tool_addition"}
    try:
        body = await request.json()
        for message_index, message in enumerate(body.get("messages", [])):
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if isinstance(block, dict):
                    block_type = block.get("type")
                    if block_type not in known_block_types:
                        unknown_block_shapes.append({
                            "message": message_index,
                            "block": block_index,
                            "type": block_type,
                            "keys": sorted(block.keys()),
                        })
    except Exception:
        unknown_block_shapes = [{"error": "request shape unavailable"}]
    logger.error(
        "Request validation failed for %s: errors=%s block_shapes=%s",
        request.url.path,
        json.dumps(diagnostics),
        json.dumps(unknown_block_shapes),
    )
    if unknown_block_shapes and "error" not in unknown_block_shapes[0]:
        types = sorted({str(shape.get("type")) for shape in unknown_block_shapes})
        message = f"Unsupported request content block type(s): {', '.join(types)}"
    else:
        first = diagnostics[0] if diagnostics else {"loc": ["body"], "msg": "Invalid request"}
        message = f"Invalid request payload at {'.'.join(map(str, first['loc']))}: {first['msg']}"
    return JSONResponse(
        status_code=400,
        content={"type": "error", "error": {"type": "invalid_request_error", "message": message}},
    )

# Allow local browser apps (SYL showcase, etc.) to call the dev proxy
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8787", "http://127.0.0.1:8787", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get API keys from environment
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Proxy authentication: if set, clients must present this key to use the proxy
PROXY_API_KEY = os.environ.get("PROXY_API_KEY")

# Azure OpenAI configuration
AZURE_API_KEY = provider_setting(PREPARED_PROFILE, "azure", "api_key", "AZURE_API_KEY")
AZURE_API_BASE = provider_setting(PREPARED_PROFILE, "azure", "api_base", "AZURE_API_BASE")
AZURE_API_VERSION = provider_setting(PREPARED_PROFILE, "azure", "api_version", "AZURE_API_VERSION", "2025-04-01-preview")
AZURE_SUBSCRIPTION_ID = provider_setting(PREPARED_PROFILE, "azure", "subscription_id", "AZURE_SUBSCRIPTION_ID")
AZURE_RESOURCE_GROUP = provider_setting(PREPARED_PROFILE, "azure", "resource_group", "AZURE_RESOURCE_GROUP")
AZURE_ACCOUNT_NAME = provider_setting(PREPARED_PROFILE, "azure", "account_name", "AZURE_ACCOUNT_NAME")
AZURE_LOCATION = provider_setting(PREPARED_PROFILE, "azure", "location", "AZURE_LOCATION", "eastus2")

# Azure AI Foundry configuration
AZURE_AI_API_KEY = provider_setting(PREPARED_PROFILE, "azure_ai", "api_key", "AZURE_AI_API_KEY", AZURE_API_KEY)
AZURE_AI_API_BASE = provider_setting(PREPARED_PROFILE, "azure_ai", "api_base", "AZURE_AI_API_BASE", AZURE_API_BASE)
AZURE_AI_API_VERSION = provider_setting(PREPARED_PROFILE, "azure_ai", "api_version", "AZURE_AI_API_VERSION", AZURE_API_VERSION)
AZURE_AI_SUBSCRIPTION_ID = provider_setting(PREPARED_PROFILE, "azure_ai", "subscription_id", "AZURE_AI_SUBSCRIPTION_ID", AZURE_SUBSCRIPTION_ID)
AZURE_AI_RESOURCE_GROUP = provider_setting(PREPARED_PROFILE, "azure_ai", "resource_group", "AZURE_AI_RESOURCE_GROUP", AZURE_RESOURCE_GROUP)
AZURE_AI_ACCOUNT_NAME = provider_setting(PREPARED_PROFILE, "azure_ai", "account_name", "AZURE_AI_ACCOUNT_NAME", AZURE_ACCOUNT_NAME)
AZURE_AI_LOCATION = provider_setting(PREPARED_PROFILE, "azure_ai", "location", "AZURE_AI_LOCATION", AZURE_LOCATION)

# Get Vertex AI project and location from environment (if set)
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "unset")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "unset")

# Option to use Gemini API key instead of ADC for Vertex AI
USE_VERTEX_AUTH = os.environ.get("USE_VERTEX_AUTH", "False").lower() == "true"

# Get OpenAI base URL from environment (if set)
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")

# Get preferred provider (default to openai)
PREFERRED_PROVIDER = (profile_value(PREPARED_PROFILE, "preferred_provider", "PREFERRED_PROVIDER", "openai") or "openai").lower()

# Get model mapping configuration from environment
# Default to latest OpenAI models if not set
BIG_MODEL = route_value(PREPARED_PROFILE, "big", "BIG_MODEL", "gpt-4.1")
SMALL_MODEL = route_value(PREPARED_PROFILE, "small", "SMALL_MODEL", "gpt-4.1-mini")
FRONTIER_MODEL = route_value(PREPARED_PROFILE, "frontier", "FRONTIER_MODEL", "gpt-6-astra")

# Opus 1M compatibility aliases follow the Codex/Azure reasoning tiers.
OPUS_1M_EFFORT_MODELS = {
    "low": "gpt-5.6-luna",
    "medium": "gpt-5.6-terra",
    "med": "gpt-5.6-terra",
    "high": "gpt-5.6-sol",
}

# Azure OpenAI's tools array limit is 128. Claude Code can expose more tools
# than that when many MCP servers are enabled, so keep the request valid for
# Azure-compatible backends while preserving deterministic ordering.
MAX_PROVIDER_TOOLS = 128
MAX_PROVIDER_TOOL_CALL_ID = 64

def normalize_provider_tool_call_id(tool_id: Any) -> str:
    """Keep Anthropic tool IDs stable while satisfying Azure's 64-char limit."""
    value = str(tool_id or "tool_call")
    if len(value) <= MAX_PROVIDER_TOOL_CALL_ID:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{value[:MAX_PROVIDER_TOOL_CALL_ID - 17]}_{digest}"

def is_provider_retryable_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in (
        "anthropicexception", "resource not found", "ratelimit", "rate limit",
        "too many requests", "429", "overloaded",
    ))

def alternate_azure_model(current_model: str, role: str) -> str:
    """Choose a live alternate deployment when the selected Azure tier is throttled."""
    clean = provider_model_identity(current_model)
    alternates = {
        "gpt-6-astra": "gpt-5.6-sol",
        "gpt-5.6-terra": "gpt-5.6-luna",
        "gpt-5.6-luna": "gpt-5.5",
        "gpt-5.6-sol": "gpt-5.6-luna",
    }
    alternate = alternates.get(clean, MODEL_ROLE_CONFIG[role]["configured_model"])
    return format_provider_model("azure", resolve_azure_deployment(alternate))

# Models currently deployed in the configured Azure OpenAI resource.
OPENAI_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5.1",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.5",
    "gpt-6-astra",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "o1",
    "o3-mini",
    "o3",
    "o4-mini",
    "codex-mini",
    "gpt-audio",
    "gpt-audio-mini",
    "model-router",
]

# List of Gemini models
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-pro"
]

# Azure OpenAI deployment map: model name -> deployment name
# Users can override via AZURE_DEPLOYMENT_MAP env var (JSON string)
_default_azure_map = {
    "gpt-4o": "gpt-4o",
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4.1": "gpt-4-1",
    "gpt-4.1-mini": "gpt-4-1-mini",
    "gpt-4.1-nano": "gpt-4-1-nano",
    "gpt-5": "gpt-5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5-nano": "gpt-5-nano",
    "gpt-5.1": "gpt-5-1",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.2": "gpt-5-2",
    "gpt-5.2-codex": "gpt-5-2-codex",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.5": "gpt-5.5",
    "gpt-6-astra": "gpt-6-astra",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "o1": "o1",
    "o3-mini": "o3-mini",
    "o3": "o3",
    "o4-mini": "o4-mini",
    "codex-mini": "codex-mini",
    "gpt-audio": "gpt-audio",
    "gpt-audio-mini": "gpt-audio-mini",
    "model-router": "model-router",
}
AZURE_DEPLOYMENT_MAP = deployment_map(
    PREPARED_PROFILE,
    PREFERRED_PROVIDER if PREFERRED_PROVIDER in ("azure", "azure_ai") else "azure",
    _default_azure_map,
)
AZURE_MODELS = list(AZURE_DEPLOYMENT_MAP.keys())

MODEL_ROLE_CONFIG = {
    "small": {
        "claude_family": "haiku",
        "env": "SMALL_MODEL",
        "configured_model": SMALL_MODEL,
        "purpose": "fast edits, summaries, lightweight tool use",
    },
    "big": {
        "claude_family": "sonnet",
        "env": "BIG_MODEL",
        "configured_model": BIG_MODEL,
        "purpose": "general coding, planning, and multi-step work",
    },
    "frontier": {
        "claude_family": "opus",
        "env": "FRONTIER_MODEL",
        "configured_model": FRONTIER_MODEL,
        "purpose": "highest-capability reasoning and difficult code work",
    },
}

PROVIDER_CAPABILITIES = {
    "anthropic": {
        "label": "Anthropic Claude passthrough",
        "configured": bool(ANTHROPIC_API_KEY),
        "env": ["ANTHROPIC_API_KEY"],
        "model_prefix": "anthropic/",
        "notes": "Use when you want exact Claude model behavior.",
    },
    "azure": {
        "label": "Azure OpenAI",
        "configured": bool(AZURE_API_KEY and AZURE_API_BASE),
        "env": ["AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION", "AZURE_DEPLOYMENT_MAP"],
        "instance": PREPARED_PROFILE.get("provider_instances", {}).get("azure", {}),
        "model_prefix": "azure/",
        "notes": "Best for deployed Azure OpenAI models with stable deployment names.",
    },
    "azure_ai": {
        "label": "Azure AI Foundry",
        "configured": bool(AZURE_AI_API_KEY and AZURE_AI_API_BASE),
        "env": ["AZURE_AI_API_KEY", "AZURE_AI_API_BASE", "AZURE_AI_API_VERSION"],
        "instance": PREPARED_PROFILE.get("provider_instances", {}).get("azure_ai", {}),
        "model_prefix": "azure_ai/",
        "notes": "Use for Foundry model endpoints and broader Azure AI catalog parity.",
    },
    "openai": {
        "label": "OpenAI",
        "configured": bool(OPENAI_API_KEY),
        "env": ["OPENAI_API_KEY", "OPENAI_BASE_URL"],
        "model_prefix": "openai/",
        "notes": "Direct OpenAI API access.",
    },
    "google": {
        "label": "Google Gemini",
        "configured": bool(GEMINI_API_KEY or (USE_VERTEX_AUTH and VERTEX_PROJECT != "unset")),
        "env": ["GEMINI_API_KEY", "USE_VERTEX_AUTH", "VERTEX_PROJECT", "VERTEX_LOCATION"],
        "model_prefix": "gemini/",
        "notes": "Gemini API key or Vertex AI ADC.",
    },
}

def infer_claude_role(model_name: str) -> Optional[str]:
    """Return the CCP role implied by a Claude-family model name."""
    clean = strip_provider_prefix(model_name).lower()
    if "haiku" in clean:
        return "small"
    if "sonnet" in clean:
        return "big"
    # Claude Code 2.1.296 added Fable/Mythos above Opus. CCP has three
    # backend tiers, so route those highest-capability families through the
    # frontier model instead of accidentally treating them as Anthropic
    # passthrough models.
    if any(family in clean for family in ("opus", "fable", "mythos")):
        return "frontier"
    return None


def normalize_reasoning_effort(effort: Any) -> Optional[str]:
    """Accept known efforts without letting malformed client values crash routing."""
    if not isinstance(effort, str):
        return None
    normalized = effort.strip().lower()
    if normalized == "med":
        normalized = "medium"
    if normalized in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        return normalized
    return None


def opus_1m_effort(model_name: str, effort: Any = None) -> Optional[str]:
    """Explicit effort wins over an alias suffix; bare Opus 1M defaults to medium."""
    clean = strip_provider_prefix(model_name).lower()
    if "opus" not in clean or "1m" not in clean:
        return None
    normalized = normalize_reasoning_effort(effort)
    if normalized:
        return normalized
    suffix = re.search(r"(?<![a-z0-9])(low|medium|med|high)(?![a-z0-9])", clean)
    return normalize_reasoning_effort(suffix.group(1)) if suffix else "medium"


def opus_1m_effort_model(model_name: str, effort: Any = None) -> Optional[str]:
    """Resolve an Opus 1M alias and effort to the 5.6 reasoning tier."""
    normalized = opus_1m_effort(model_name, effort)
    if normalized is None:
        return None
    if normalized in ("none", "minimal"):
        normalized = "low"
    elif normalized in ("xhigh", "max", "ultra"):
        normalized = "high"
    return OPUS_1M_EFFORT_MODELS[normalized]


def map_role_to_provider_model(role: str, provider: Optional[str] = None) -> str:
    """Map CCP small/big/frontier roles to the configured backend model."""
    target_provider = (provider or PREFERRED_PROVIDER).lower()
    configured = MODEL_ROLE_CONFIG[role]["configured_model"]
    if target_provider in ("azure", "azure_ai"):
        return format_provider_model(target_provider, resolve_azure_deployment(configured))
    if target_provider == "google" and configured in GEMINI_MODELS:
        return format_provider_model("gemini", configured)
    if target_provider == "anthropic":
        family = MODEL_ROLE_CONFIG[role]["claude_family"]
        return f"anthropic/claude-{family}"
    return format_provider_model("openai", configured)

def current_model_routes() -> Dict[str, Any]:
    """Return the active Claude-family to backend model routing table."""
    routes = {}
    for role, config in MODEL_ROLE_CONFIG.items():
        routes[role] = {
            **config,
            "provider": PREFERRED_PROVIDER,
            "routed_model": map_role_to_provider_model(role),
        }
    return routes


def route_openai_compatible_model(model_name: str, reasoning_effort: Any = None) -> str:
    """Resolve an OpenAI-compatible client model through CCP's configured routes."""
    if model_name.startswith(("anthropic/", "openai/", "gemini/", "azure/", "azure_ai/")):
        return model_name

    clean_name = strip_provider_prefix(model_name)
    if PREFERRED_PROVIDER == "anthropic":
        return format_provider_model("anthropic", clean_name)

    opus_1m_model = opus_1m_effort_model(clean_name, reasoning_effort)
    if opus_1m_model and PREFERRED_PROVIDER in ("openai", "azure", "azure_ai"):
        target = resolve_azure_deployment(opus_1m_model) if PREFERRED_PROVIDER in ("azure", "azure_ai") else opus_1m_model
        return format_provider_model(PREFERRED_PROVIDER, target)

    role = infer_claude_role(clean_name)
    if role:
        return map_role_to_provider_model(role)
    if clean_name in GEMINI_MODELS:
        return format_provider_model("gemini", clean_name)
    if clean_name in (AZURE_MODELS + OPENAI_MODELS) and PREFERRED_PROVIDER in ("azure", "azure_ai"):
        return format_provider_model(PREFERRED_PROVIDER, resolve_azure_deployment(clean_name))
    if clean_name in OPENAI_MODELS:
        return format_provider_model("openai", clean_name)
    return model_name


def configure_provider_credentials(payload: Dict[str, Any], client_api_key: Optional[str] = None) -> None:
    """Attach server-managed upstream credentials without exposing them to clients."""
    model = payload.get("model", "")
    if model.startswith("azure_ai/"):
        payload.update(
            api_key=AZURE_AI_API_KEY,
            api_base=AZURE_AI_API_BASE,
            api_version=AZURE_AI_API_VERSION,
            custom_llm_provider="azure_ai",
        )
    elif model.startswith("azure/"):
        payload.update(api_key=AZURE_API_KEY, api_base=AZURE_API_BASE, api_version=AZURE_API_VERSION)
    elif model.startswith("openai/"):
        payload["api_key"] = OPENAI_API_KEY
        if OPENAI_BASE_URL:
            payload["api_base"] = OPENAI_BASE_URL
    elif model.startswith("gemini/"):
        if USE_VERTEX_AUTH:
            payload.update(
                vertex_project=VERTEX_PROJECT,
                vertex_location=VERTEX_LOCATION,
                custom_llm_provider="vertex_ai",
            )
        else:
            payload["api_key"] = GEMINI_API_KEY
    elif model.startswith("anthropic/"):
        effective_key = ANTHROPIC_API_KEY or client_api_key
        if effective_key:
            payload["api_key"] = effective_key


def apply_group_route(payload: Dict[str, Any], requested_model: str) -> Optional[JSONResponse]:
    """Send models from non-legacy CCP groups to their own Azure account; legacy routing is untouched."""
    route = ccp_lifecycle.manager.route(requested_model, AZURE_API_VERSION)
    if not route:
        return None
    group_id = route.pop("ccp_group")
    if not route.get("api_key"):
        return JSONResponse(status_code=503, content={"error": {
            "type": "group_credentials_missing",
            "message": f"No API credential for CCP group {group_id}; add AZURE_GROUP_<ID>_API_KEY or check az login.",
            "group": group_id,
        }})
    for key in ("api_key", "api_base", "api_version", "custom_llm_provider"):
        payload.pop(key, None)
    payload.update(route)
    logger.debug(f"CCP group route: {requested_model} -> {route['model']} via group {group_id}")
    return None


def openai_response_dict(response: Any) -> Dict[str, Any]:
    """Convert LiteLLM response objects into JSON-safe OpenAI response dictionaries."""
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        try:
            return response.model_dump(mode="json")
        except TypeError:
            return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    raise TypeError(f"Unsupported LiteLLM response type: {type(response).__name__}")

def strip_provider_prefix(model_name: str) -> str:
    """Strip supported provider prefixes from a model name."""
    for prefix in ("anthropic/", "openai/", "gemini/", "azure/", "azure_ai/"):
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name


def format_provider_model(provider: str, model_name: str) -> str:
    """Format the model name with the correct provider prefix."""
    clean_name = strip_provider_prefix(model_name)
    if provider == "anthropic":
        return f"anthropic/{clean_name}"
    if provider == "openai":
        return f"openai/{clean_name}"
    if provider == "gemini":
        return f"gemini/{clean_name}"
    if provider == "azure":
        return f"azure/{clean_name}"
    if provider == "azure_ai":
        return f"azure_ai/{clean_name}"
    return model_name


def resolve_azure_deployment(model_name: str) -> str:
    """Resolve an Azure deployment name for a custom model."""
    normalized = strip_provider_prefix(model_name)
    return AZURE_DEPLOYMENT_MAP.get(normalized, normalized)


def provider_model_identity(model_name: str) -> str:
    """Recover the canonical model even when Azure uses a custom deployment name."""
    clean = strip_provider_prefix(model_name).removeprefix("responses/")
    if model_name.startswith(("azure/", "azure_ai/")):
        for canonical, deployment in AZURE_DEPLOYMENT_MAP.items():
            if clean == deployment:
                return canonical
    return clean

# Helper function to clean schema for Gemini
def clean_gemini_schema(schema: Any) -> Any:
    """Recursively removes unsupported fields from a JSON schema for Gemini."""
    if isinstance(schema, dict):
        # Remove specific keys unsupported by Gemini tool parameters
        schema.pop("additionalProperties", None)
        schema.pop("default", None)

        # Check for unsupported 'format' in string types
        if schema.get("type") == "string" and "format" in schema:
            allowed_formats = {"enum", "date-time"}
            if schema["format"] not in allowed_formats:
                logger.debug(f"Removing unsupported format '{schema['format']}' for string type in Gemini schema.")
                schema.pop("format")

        # Recursively clean nested schemas (properties, items, etc.)
        for key, value in list(schema.items()): # Use list() to allow modification during iteration
            schema[key] = clean_gemini_schema(value)
    elif isinstance(schema, list):
        # Recursively clean items in a list
        return [clean_gemini_schema(item) for item in schema]
    return schema


def normalize_strict_response_schema(schema: Any) -> Any:
    """Make JSON schemas valid for OpenAI/Azure strict structured outputs."""
    if isinstance(schema, dict):
        normalized = {key: normalize_strict_response_schema(value) for key, value in schema.items()}
        if isinstance(normalized.get("properties"), dict):
            normalized["required"] = list(normalized["properties"].keys())
            normalized.setdefault("additionalProperties", False)
        return normalized
    if isinstance(schema, list):
        return [normalize_strict_response_schema(item) for item in schema]
    return schema

# Models for Anthropic API requests
class ContentBlockText(BaseModel):
    type: Literal["text"]
    text: str

class ContentBlockImage(BaseModel):
    type: Literal["image"]
    source: Dict[str, Any]

class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]

class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], Dict[str, Any], List[Any], Any]

class ContentBlockThinking(BaseModel):
    type: Literal["thinking"]
    thinking: str

class ContentBlockToolAddition(BaseModel):
    """Claude Code resume metadata for a tool added after the session began."""
    type: Literal["tool_addition"]
    tool: Dict[str, Any]

class SystemContent(BaseModel):
    type: Literal["text"]
    text: str

class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: Union[str, List[Union[ContentBlockText, ContentBlockImage, ContentBlockToolUse, ContentBlockToolResult, ContentBlockThinking, ContentBlockToolAddition]]]

class Tool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]

class ThinkingConfig(BaseModel):
    enabled: bool = True

class MessagesRequest(BaseModel):
    output_config: Optional[Dict[str, Any]] = None
    model: str
    max_tokens: int
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[ThinkingConfig] = None
    original_model: Optional[str] = None  # Will store the original model name
    
    @field_validator('model')
    def validate_model_field(cls, v, info): # Renamed to avoid conflict
        original_model = v
        new_model = v # Default to original value

        logger.debug(f"📋 MODEL VALIDATION: Original='{original_model}', Preferred='{PREFERRED_PROVIDER}', BIG='{BIG_MODEL}', SMALL='{SMALL_MODEL}', FRONTIER='{FRONTIER_MODEL}'")

        # Remove provider prefixes for easier matching
        clean_v = strip_provider_prefix(v)

        # --- Mapping Logic --- START ---
        mapped = False
        if PREFERRED_PROVIDER == "anthropic":
            # Don't remap to big/small models, just add the prefix.
            new_model = f"anthropic/{clean_v}"
            mapped = True

        role = infer_claude_role(clean_v)
        output_config = info.data.get("output_config") or {}
        requested_effort = output_config.get("effort") if isinstance(output_config, dict) else None
        opus_1m_model = opus_1m_effort_model(clean_v, requested_effort)
        if not mapped and opus_1m_model and PREFERRED_PROVIDER in ("openai", "azure", "azure_ai"):
            new_model = format_provider_model(
                PREFERRED_PROVIDER,
                resolve_azure_deployment(opus_1m_model)
                if PREFERRED_PROVIDER in ("azure", "azure_ai")
                else opus_1m_model,
            )
            mapped = True
        elif not mapped and role:
            new_model = map_role_to_provider_model(role)
            mapped = True

        # Add prefixes to non-mapped models if they match known lists
        elif not mapped:
            if clean_v in GEMINI_MODELS and not v.startswith('gemini/'):
                new_model = f"gemini/{clean_v}"
                mapped = True
            elif clean_v in (AZURE_MODELS + OPENAI_MODELS) and PREFERRED_PROVIDER in ("azure", "azure_ai"):
                deployment = AZURE_DEPLOYMENT_MAP.get(clean_v, clean_v)
                new_model = format_provider_model(PREFERRED_PROVIDER, deployment)
                mapped = True
            elif clean_v in OPENAI_MODELS and not v.startswith('openai/'):
                new_model = f"openai/{clean_v}"
                mapped = True
        # --- Mapping Logic --- END ---

        if mapped:
            logger.debug(f"📌 MODEL MAPPING: '{original_model}' ➡️ '{new_model}'")
        else:
             # If no mapping occurred and no prefix exists, log warning or decide default
             if not v.startswith(('openai/', 'gemini/', 'anthropic/', 'azure/', 'azure_ai/')):
                 logger.warning(f"⚠️ No prefix or mapping rule for model: '{original_model}'. Using as is.")
             new_model = v # Ensure we return the original if no rule applied

        # Store the original model in the values dictionary
        values = info.data
        if isinstance(values, dict):
            values['original_model'] = original_model

        return new_model

class TokenCountRequest(BaseModel):
    output_config: Optional[Dict[str, Any]] = None
    model: str
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    tools: Optional[List[Tool]] = None
    thinking: Optional[ThinkingConfig] = None
    tool_choice: Optional[Dict[str, Any]] = None
    original_model: Optional[str] = None  # Will store the original model name
    
    @field_validator('model')
    def validate_model_token_count(cls, v, info): # Renamed to avoid conflict
        # Use the same logic as MessagesRequest validator
        # NOTE: Pydantic validators might not share state easily if not class methods
        # Re-implementing the logic here for clarity, could be refactored
        original_model = v
        new_model = v # Default to original value

        logger.debug(f"📋 TOKEN COUNT VALIDATION: Original='{original_model}', Preferred='{PREFERRED_PROVIDER}', BIG='{BIG_MODEL}', SMALL='{SMALL_MODEL}', FRONTIER='{FRONTIER_MODEL}'")

        # Remove provider prefixes for easier matching
        clean_v = strip_provider_prefix(v)

        # --- Mapping Logic --- START ---
        mapped = False
        role = infer_claude_role(clean_v)
        output_config = info.data.get("output_config") or {}
        opus_1m_model = opus_1m_effort_model(clean_v, output_config.get("effort"))
        if PREFERRED_PROVIDER == "anthropic":
            new_model = format_provider_model("anthropic", clean_v)
            mapped = True
        elif opus_1m_model and PREFERRED_PROVIDER in ("openai", "azure", "azure_ai"):
            new_model = format_provider_model(
                PREFERRED_PROVIDER,
                resolve_azure_deployment(opus_1m_model)
                if PREFERRED_PROVIDER in ("azure", "azure_ai") else opus_1m_model,
            )
            mapped = True
        elif role:
            new_model = map_role_to_provider_model(role)
            mapped = True

        # Add prefixes to non-mapped models if they match known lists
        elif not mapped:
            if clean_v in GEMINI_MODELS and not v.startswith('gemini/'):
                new_model = f"gemini/{clean_v}"
                mapped = True
            elif clean_v in (AZURE_MODELS + OPENAI_MODELS) and PREFERRED_PROVIDER in ("azure", "azure_ai"):
                deployment = AZURE_DEPLOYMENT_MAP.get(clean_v, clean_v)
                new_model = format_provider_model(PREFERRED_PROVIDER, deployment)
                mapped = True
            elif clean_v in OPENAI_MODELS and not v.startswith('openai/'):
                new_model = f"openai/{clean_v}"
                mapped = True
        # --- Mapping Logic --- END ---

        if mapped:
            logger.debug(f"📌 TOKEN COUNT MAPPING: '{original_model}' ➡️ '{new_model}'")
        else:
             if not v.startswith(('openai/', 'gemini/', 'anthropic/', 'azure/', 'azure_ai/')):
                 logger.warning(f"⚠️ No prefix or mapping rule for token count model: '{original_model}'. Using as is.")
             new_model = v # Ensure we return the original if no rule applied

        # Store the original model in the values dictionary
        values = info.data
        if isinstance(values, dict):
            values['original_model'] = original_model

        return new_model

class TokenCountResponse(BaseModel):
    input_tokens: int


class ProviderRequestContext:
    """Minimal request metadata shared with provider-specific payload preparation."""

    def __init__(self, model: str, original_model: str, reasoning_effort: Any = None):
        self.model = model
        self.original_model = original_model
        self.output_config = {"effort": reasoning_effort} if reasoning_effort is not None else {}

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

class MessagesResponse(BaseModel):
    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: List[Union[ContentBlockText, ContentBlockToolUse]]
    type: Literal["message"] = "message"
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]] = None
    stop_sequence: Optional[str] = None
    usage: Usage


def apply_context_window_route(request: Union[MessagesRequest, TokenCountRequest], beta_header: str) -> None:
    """Claude Code may express opus[1m] using the context-1m beta header."""
    original = request.original_model or request.model
    if (
        PREFERRED_PROVIDER not in ("openai", "azure", "azure_ai")
        or infer_claude_role(original) != "frontier"
        or not any(beta.strip().startswith("context-1m-") for beta in beta_header.lower().split(","))
    ):
        return
    effort = (request.output_config or {}).get("effort")
    backend = opus_1m_effort_model(original + "[1m]", effort)
    request.model = format_provider_model(
        PREFERRED_PROVIDER,
        resolve_azure_deployment(backend) if PREFERRED_PROVIDER in ("azure", "azure_ai") else backend,
    )
    if not normalize_reasoning_effort(effort):
        request.output_config = {**(request.output_config or {}), "effort": "medium"}


def prepare_provider_request(payload: Dict[str, Any], request: MessagesRequest, *, resolve_transport: bool = True) -> None:
    """Translate Anthropic-only options for the actual destination, including retries."""
    output_config = request.output_config or {}
    payload.pop("output_config", None)
    payload.pop("reasoning_effort", None)
    if not payload.get("tools"):
        payload.pop("tool_choice", None)
    if payload["model"].startswith("anthropic/"):
        if output_config:
            payload["output_config"] = output_config
        return

    payload.pop("thinking", None)
    if not payload["model"].startswith(("openai/", "azure/", "azure_ai/")):
        return

    canonical = provider_model_identity(payload["model"])
    effort = normalize_reasoning_effort(output_config.get("effort"))
    effort = effort or opus_1m_effort(request.original_model or request.model)
    if effort and re.match(r"^(gpt-[56](?:[.-]|$)|o[134](?:-|$))", canonical):
        payload["reasoning_effort"] = effort

    output_format = output_config.get("format")
    if isinstance(output_format, dict) and output_format.get("type") == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "strict": True,
                "schema": normalize_strict_response_schema(output_format["schema"]),
            },
        }

    needs_responses = canonical == "gpt-6-astra" or (
        canonical.startswith("gpt-5.6-") and bool(payload.get("tools"))
    )
    if needs_responses:
        # Astra and 5.6 reasoning tool calls require Responses. The pinned
        # LiteLLM catalog predates these models, so explicitly allow effort.
        payload["allowed_openai_params"] = ["reasoning_effort", "tools", "tool_choice", "response_format"]
        for key in ("temperature", "top_p", "top_k", "logprobs", "top_logprobs", "stop"):
            payload.pop(key, None)
        if effort in ("none", "minimal"):
            payload["reasoning_effort"] = "low"
        elif effort in ("max", "ultra"):
            # The pinned bridge recognizes newer efforts when supplied as a dict.
            payload["reasoning_effort"] = {"effort": effort}
        if resolve_transport:
            provider, _, deployment = payload["model"].partition("/")
            deployment = deployment.removeprefix("responses/")
            if provider == "azure_ai":
                # Use the same Foundry account/key via its Azure OpenAI endpoint.
                provider = "azure"
                payload["api_base"] = payload["api_base"].rstrip("/").removesuffix("/models")
            payload["model"] = f"{provider}/responses/{deployment}"
            # Preserve the provider through LiteLLM's nested Responses call;
            # otherwise it can infer OpenAI and send Azure requests to /responses.
            payload["custom_llm_provider"] = provider
            payload.setdefault("metadata", {})
            payload.setdefault("litellm_metadata", {})

def extract_client_api_key(request: Request) -> Optional[str]:
    """Extract the API key from the client request headers (x-api-key or Authorization: Bearer)."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None

@app.middleware("http")
async def auth_and_log(request: Request, call_next):
    method = request.method
    path = request.url.path

    # Skip auth for non-API routes (healthcheck, root, docs)
    if path.startswith("/v1/") or path in ("/chat/completions", "/models"):
        client_key = extract_client_api_key(request)

        # If PROXY_API_KEY is configured, require it
        if PROXY_API_KEY and client_key != PROXY_API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"type": "authentication_error", "message": "Invalid or missing API key"}},
            )

        # Stash the client key so endpoints can use it for passthrough
        request.state.client_api_key = client_key

    logger.debug(f"Request: {method} {path}")
    response = await call_next(request)
    return response

def parse_tool_result_content(content):
    """Helper function to properly parse and normalize tool result content."""
    if content is None:
        return "No content provided"
        
    if isinstance(content, str):
        return content
        
    if isinstance(content, list):
        result = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                result += item.get("text", "") + "\n"
            elif isinstance(item, str):
                result += item + "\n"
            elif isinstance(item, dict):
                if "text" in item:
                    result += item.get("text", "") + "\n"
                else:
                    try:
                        result += json.dumps(item) + "\n"
                    except:
                        result += str(item) + "\n"
            else:
                try:
                    result += str(item) + "\n"
                except:
                    result += "Unparseable content\n"
        return result.strip()
        
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text", "")
        try:
            return json.dumps(content)
        except:
            return str(content)
            
    # Fallback for any other type
    try:
        return str(content)
    except:
        return "Unparseable content"

def convert_anthropic_to_litellm(anthropic_request: MessagesRequest) -> Dict[str, Any]:
    """Convert Anthropic API request format to LiteLLM format (which follows OpenAI)."""
    # LiteLLM already handles Anthropic models when using the format model="anthropic/claude-3-opus-20240229"
    # So we just need to convert our Pydantic model to a dict in the expected format
    
    messages = []
    
    # Add system message if present
    if anthropic_request.system:
        # Handle different formats of system messages
        if isinstance(anthropic_request.system, str):
            # Simple string format
            messages.append({"role": "system", "content": anthropic_request.system})
        elif isinstance(anthropic_request.system, list):
            # List of content blocks
            system_text = ""
            for block in anthropic_request.system:
                if hasattr(block, 'type') and block.type == "text":
                    system_text += block.text + "\n\n"
                elif isinstance(block, dict) and block.get("type") == "text":
                    system_text += block.get("text", "") + "\n\n"
            
            if system_text:
                messages.append({"role": "system", "content": system_text.strip()})
    
    # Add conversation messages
    for idx, msg in enumerate(anthropic_request.messages):
        content = msg.content
        if msg.role == "system":
            if isinstance(content, str):
                system_text = content
            else:
                system_text = "\n\n".join(
                    getattr(block, "text", getattr(block, "thinking", ""))
                    for block in content
                    if getattr(block, "type", None) in ("text", "thinking")
                )
            if system_text.strip():
                messages.append({"role": "system", "content": system_text.strip()})
            continue
        if isinstance(content, str):
            messages.append({"role": msg.role, "content": content})
        else:
            # Special handling for tool_result in user messages
            # OpenAI/LiteLLM format expects the assistant to call the tool, 
            # and the user's next message to include the result as plain text
            if msg.role == "user" and any(block.type == "tool_result" for block in content if hasattr(block, "type")):
                # For user messages with tool_result, split into separate messages
                text_content = ""
                
                # Extract all text parts and concatenate them
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text_content += block.text + "\n"
                        elif block.type == "tool_result":
                            # Add tool result as a message by itself - simulate the normal flow
                            tool_id = normalize_provider_tool_call_id(block.tool_use_id if hasattr(block, "tool_use_id") else "")
                            
                            # Handle different formats of tool result content
                            result_content = ""
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    result_content = block.content
                                elif isinstance(block.content, list):
                                    # If content is a list of blocks, extract text from each
                                    for content_block in block.content:
                                        if hasattr(content_block, "type") and content_block.type == "text":
                                            result_content += content_block.text + "\n"
                                        elif isinstance(content_block, dict) and content_block.get("type") == "text":
                                            result_content += content_block.get("text", "") + "\n"
                                        elif isinstance(content_block, dict):
                                            # Handle any dict by trying to extract text or convert to JSON
                                            if "text" in content_block:
                                                result_content += content_block.get("text", "") + "\n"
                                            else:
                                                try:
                                                    result_content += json.dumps(content_block) + "\n"
                                                except:
                                                    result_content += str(content_block) + "\n"
                                elif isinstance(block.content, dict):
                                    # Handle dictionary content
                                    if block.content.get("type") == "text":
                                        result_content = block.content.get("text", "")
                                    else:
                                        try:
                                            result_content = json.dumps(block.content)
                                        except:
                                            result_content = str(block.content)
                                else:
                                    # Handle any other type by converting to string
                                    try:
                                        result_content = str(block.content)
                                    except:
                                        result_content = "Unparseable content"
                            
                            # OpenAI/Azure requires one role=tool message per
                            # assistant tool_call, keyed by tool_call_id.
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "content": result_content or "",
                            })

                # Preserve any ordinary text accompanying the tool results.
                if text_content.strip():
                    messages.append({"role": "user", "content": text_content.strip()})
            else:
                # Regular handling for other message types
                processed_content = []
                processed_tool_calls = []
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            processed_content.append({"type": "text", "text": block.text})
                        elif block.type == "thinking":
                            processed_content.append({"type": "text", "text": block.thinking})
                        elif block.type == "image":
                            processed_content.append({"type": "image", "source": block.source})
                        elif block.type == "tool_use":
                            # Anthropic tool_use blocks become OpenAI assistant
                            # tool_calls; they cannot appear in message.content.
                            processed_tool_calls.append({
                                "id": normalize_provider_tool_call_id(block.id),
                                "type": "function",
                                "function": {
                                    "name": block.name,
                                    "arguments": json.dumps(block.input),
                                },
                            })
                        elif block.type == "tool_result":
                            # Handle different formats of tool result content
                            processed_content_block = {
                                "type": "tool_result",
                                "tool_use_id": normalize_provider_tool_call_id(block.tool_use_id if hasattr(block, "tool_use_id") else "")
                            }
                            
                            # Process the content field properly
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    # If it's a simple string, create a text block for it
                                    processed_content_block["content"] = [{"type": "text", "text": block.content}]
                                elif isinstance(block.content, list):
                                    # If it's already a list of blocks, keep it
                                    processed_content_block["content"] = block.content
                                else:
                                    # Default fallback
                                    processed_content_block["content"] = [{"type": "text", "text": str(block.content)}]
                            else:
                                # Default empty content
                                processed_content_block["content"] = [{"type": "text", "text": ""}]
                                
                            processed_content.append(processed_content_block)
                
                message = {"role": msg.role, "content": processed_content}
                if processed_tool_calls:
                    message["tool_calls"] = processed_tool_calls
                    if not processed_content:
                        message["content"] = None
                messages.append(message)
    
    # Cap max_tokens for OpenAI/Azure models to their limit of 16384
    max_tokens = anthropic_request.max_tokens
    if (
        anthropic_request.model.startswith("openai/")
        or anthropic_request.model.startswith("gemini/")
        or anthropic_request.model.startswith("azure/")
        or anthropic_request.model.startswith("azure_ai/")
    ):
        max_tokens = min(max_tokens, 16384)
        logger.debug(f"Capping max_tokens to 16384 for OpenAI/Gemini/Azure model (original value: {anthropic_request.max_tokens})")
    
    # Create LiteLLM request dict
    litellm_request = {
        "model": anthropic_request.model,  # it understands "anthropic/claude-x" format
        "messages": messages,
        "max_completion_tokens": max_tokens,
        "temperature": anthropic_request.temperature,
        "stream": anthropic_request.stream,
        # Let CCP switch deployments immediately on Azure 429s instead of
        # waiting through LiteLLM's internal retry loop.
        "num_retries": 0,
    }
    # Only include thinking field for Anthropic models
    if anthropic_request.thinking and anthropic_request.model.startswith("anthropic/"):
        litellm_request["thinking"] = anthropic_request.thinking

    # Add optional parameters if present
    if anthropic_request.stop_sequences:
        litellm_request["stop"] = anthropic_request.stop_sequences
    
    if anthropic_request.top_p:
        litellm_request["top_p"] = anthropic_request.top_p
    
    if anthropic_request.top_k:
        litellm_request["top_k"] = anthropic_request.top_k
    
    # Convert tools to OpenAI format
    if anthropic_request.tools:
        source_tools = anthropic_request.tools
        if len(source_tools) > MAX_PROVIDER_TOOLS and not anthropic_request.model.startswith("anthropic/"):
            logger.warning(
                "Tool list has %d entries for %s; truncating to provider limit of %d",
                len(source_tools), anthropic_request.model, MAX_PROVIDER_TOOLS,
            )
            source_tools = source_tools[:MAX_PROVIDER_TOOLS]
        openai_tools = []
        is_gemini_model = anthropic_request.model.startswith("gemini/")

        for tool in source_tools:
            # Convert to dict if it's a pydantic model
            if hasattr(tool, 'dict'):
                tool_dict = tool.dict()
            else:
                # Ensure tool_dict is a dictionary, handle potential errors if 'tool' isn't dict-like
                try:
                    tool_dict = dict(tool) if not isinstance(tool, dict) else tool
                except (TypeError, ValueError):
                     logger.error(f"Could not convert tool to dict: {tool}")
                     continue # Skip this tool if conversion fails

            # Clean the schema if targeting a Gemini model
            input_schema = tool_dict.get("input_schema", {})
            if is_gemini_model:
                 logger.debug(f"Cleaning schema for Gemini tool: {tool_dict.get('name')}")
                 input_schema = clean_gemini_schema(input_schema)

            # Create OpenAI-compatible function tool
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool_dict["name"],
                    "description": tool_dict.get("description", ""),
                    "parameters": input_schema # Use potentially cleaned schema
                }
            }
            openai_tools.append(openai_tool)

        litellm_request["tools"] = openai_tools
    
    # Convert tool_choice to OpenAI format if present
    if anthropic_request.tool_choice:
        if hasattr(anthropic_request.tool_choice, 'dict'):
            tool_choice_dict = anthropic_request.tool_choice.dict()
        else:
            tool_choice_dict = anthropic_request.tool_choice
            
        # Handle Anthropic's tool_choice format
        choice_type = tool_choice_dict.get("type")
        if choice_type == "auto":
            litellm_request["tool_choice"] = "auto"
        elif choice_type == "any":
            litellm_request["tool_choice"] = "required"
        elif choice_type == "tool" and "name" in tool_choice_dict:
            litellm_request["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice_dict["name"]}
            }
        else:
            # Default to auto if we can't determine
            litellm_request["tool_choice"] = "auto"
    
    prepare_provider_request(litellm_request, anthropic_request, resolve_transport=False)
    return litellm_request

def convert_litellm_to_anthropic(litellm_response: Union[Dict[str, Any], Any], 
                                 original_request: MessagesRequest) -> MessagesResponse:
    """Convert LiteLLM (OpenAI format) response to Anthropic API response format."""
    
    # Enhanced response extraction with better error handling
    try:
        # Handle ModelResponse object from LiteLLM
        if hasattr(litellm_response, 'choices') and hasattr(litellm_response, 'usage'):
            # Extract data from ModelResponse object directly
            choices = litellm_response.choices
            message = choices[0].message if choices and len(choices) > 0 else None
            content_text = message.content if message and hasattr(message, 'content') else ""
            tool_calls = message.tool_calls if message and hasattr(message, 'tool_calls') else None
            finish_reason = choices[0].finish_reason if choices and len(choices) > 0 else "stop"
            usage_info = litellm_response.usage
            response_id = getattr(litellm_response, 'id', f"msg_{uuid.uuid4()}")
        else:
            # For backward compatibility - handle dict responses
            # If response is a dict, use it, otherwise try to convert to dict
            try:
                response_dict = litellm_response if isinstance(litellm_response, dict) else litellm_response.dict()
            except AttributeError:
                # If .dict() fails, try to use model_dump or __dict__ 
                try:
                    response_dict = litellm_response.model_dump() if hasattr(litellm_response, 'model_dump') else litellm_response.__dict__
                except AttributeError:
                    # Fallback - manually extract attributes
                    response_dict = {
                        "id": getattr(litellm_response, 'id', f"msg_{uuid.uuid4()}"),
                        "choices": getattr(litellm_response, 'choices', [{}]),
                        "usage": getattr(litellm_response, 'usage', {})
                    }
                    
            # Extract the content from the response dict
            choices = response_dict.get("choices", [{}])
            message = choices[0].get("message", {}) if choices and len(choices) > 0 else {}
            content_text = message.get("content", "")
            tool_calls = message.get("tool_calls", None)
            finish_reason = choices[0].get("finish_reason", "stop") if choices and len(choices) > 0 else "stop"
            usage_info = response_dict.get("usage", {})
            response_id = response_dict.get("id", f"msg_{uuid.uuid4()}")
        
        # Create content list for Anthropic format
        content = []
        
        # Add text content block if present (text might be None or empty for pure tool call responses)
        if content_text is not None and content_text != "":
            content.append({"type": "text", "text": content_text})
        
        # Clients speak Anthropic regardless of which backend generated the call.
        if tool_calls:
            logger.debug(f"Processing tool calls: {tool_calls}")
            
            # Convert to list if it's not already
            if not isinstance(tool_calls, list):
                tool_calls = [tool_calls]
                
            for idx, tool_call in enumerate(tool_calls):
                logger.debug(f"Processing tool call {idx}: {tool_call}")
                
                # Extract function data based on whether it's a dict or object
                if isinstance(tool_call, dict):
                    function = tool_call.get("function", {})
                    tool_id = tool_call.get("id", f"tool_{uuid.uuid4()}")
                    name = function.get("name", "")
                    arguments = function.get("arguments", "{}")
                else:
                    function = getattr(tool_call, "function", None)
                    tool_id = getattr(tool_call, "id", f"tool_{uuid.uuid4()}")
                    name = getattr(function, "name", "") if function else ""
                    arguments = getattr(function, "arguments", "{}") if function else "{}"
                
                # Convert string arguments to dict if needed
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse tool arguments as JSON: {arguments}")
                        arguments = {"raw": arguments}
                
                logger.debug(f"Adding tool_use block: id={tool_id}, name={name}, input={arguments}")
                
                content.append({
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": arguments
                })
        # Get usage information - extract values safely from object or dict
        if isinstance(usage_info, dict):
            prompt_tokens = usage_info.get("prompt_tokens", 0)
            completion_tokens = usage_info.get("completion_tokens", 0)
        else:
            prompt_tokens = getattr(usage_info, "prompt_tokens", 0)
            completion_tokens = getattr(usage_info, "completion_tokens", 0)
        
        # Map OpenAI finish_reason to Anthropic stop_reason
        stop_reason = None
        if finish_reason == "stop":
            stop_reason = "end_turn"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"  # Default
        
        # Make sure content is never empty
        if not content:
            content.append({"type": "text", "text": "..."})
        
        # Create Anthropic-style response
        anthropic_response = MessagesResponse(
            id=response_id,
            model=original_request.original_model or original_request.model,
            role="assistant",
            content=content,
            stop_reason=stop_reason,
            stop_sequence=None,
            usage=Usage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens
            )
        )
        
        return anthropic_response
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        error_message = f"Error converting response: {str(e)}\n\nFull traceback:\n{error_traceback}"
        logger.error(error_message)
        
        # In case of any error, create a fallback response
        return MessagesResponse(
            id=f"msg_{uuid.uuid4()}",
            model=original_request.model,
            role="assistant",
            content=[{"type": "text", "text": f"Error converting response: {str(e)}. Please check server logs."}],
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0)
        )

async def handle_streaming(response_generator, original_request: MessagesRequest):
    """Handle streaming responses from LiteLLM and convert to Anthropic format."""
    try:
        # Send message_start event
        message_id = f"msg_{uuid.uuid4().hex[:24]}"  # Format similar to Anthropic's IDs
        
        message_data = {
            'type': 'message_start',
            'message': {
                'id': message_id,
                'type': 'message',
                'role': 'assistant',
                'model': original_request.model,
                'content': [],
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {
                    'input_tokens': 0,
                    'cache_creation_input_tokens': 0,
                    'cache_read_input_tokens': 0,
                    'output_tokens': 0
                }
            }
        }
        yield f"event: message_start\ndata: {json.dumps(message_data)}\n\n"
        
        # Content block index for the first text block
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        
        # Send a ping to keep the connection alive (Anthropic does this)
        yield f"event: ping\ndata: {json.dumps({'type': 'ping'})}\n\n"
        
        tool_index = None
        current_tool_call = None
        tool_content = ""
        accumulated_text = ""  # Track accumulated text content
        text_sent = False  # Track if we've sent any text content
        text_block_closed = False  # Track if text block is closed
        input_tokens = 0
        output_tokens = 0
        has_sent_stop_reason = False
        last_tool_index = 0
        
        # Process each chunk
        async for chunk in response_generator:
            try:

                
                # Check if this is the end of the response with usage data
                if hasattr(chunk, 'usage') and chunk.usage is not None:
                    if hasattr(chunk.usage, 'prompt_tokens'):
                        input_tokens = chunk.usage.prompt_tokens
                    if hasattr(chunk.usage, 'completion_tokens'):
                        output_tokens = chunk.usage.completion_tokens
                
                # Handle text content
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    choice = chunk.choices[0]
                    
                    # Get the delta from the choice
                    if hasattr(choice, 'delta'):
                        delta = choice.delta
                    else:
                        # If no delta, try to get message
                        delta = getattr(choice, 'message', {})
                    
                    # Check for finish_reason to know when we're done
                    finish_reason = getattr(choice, 'finish_reason', None)
                    
                    # Process text content
                    delta_content = None
                    
                    # Handle different formats of delta content
                    if hasattr(delta, 'content'):
                        delta_content = delta.content
                    elif isinstance(delta, dict) and 'content' in delta:
                        delta_content = delta['content']
                    
                    # Accumulate text content
                    if delta_content is not None and delta_content != "":
                        accumulated_text += delta_content
                        
                        # Always emit text deltas if no tool calls started
                        if tool_index is None and not text_block_closed:
                            text_sent = True
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta_content}})}\n\n"
                    
                    # Process tool calls
                    delta_tool_calls = None
                    
                    # Handle different formats of tool calls
                    if hasattr(delta, 'tool_calls'):
                        delta_tool_calls = delta.tool_calls
                    elif isinstance(delta, dict) and 'tool_calls' in delta:
                        delta_tool_calls = delta['tool_calls']
                    
                    # Process tool calls if any
                    if delta_tool_calls:
                        # First tool call we've seen - need to handle text properly
                        if tool_index is None:
                            # If we've been streaming text, close that text block
                            if text_sent and not text_block_closed:
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            # If we've accumulated text but not sent it, we need to emit it now
                            # This handles the case where the first delta has both text and a tool call
                            elif accumulated_text and not text_sent and not text_block_closed:
                                # Send the accumulated text
                                text_sent = True
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': accumulated_text}})}\n\n"
                                # Close the text block
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                            # Close text block even if we haven't sent anything - models sometimes emit empty text blocks
                            elif not text_block_closed:
                                text_block_closed = True
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                                
                        # Convert to list if it's not already
                        if not isinstance(delta_tool_calls, list):
                            delta_tool_calls = [delta_tool_calls]
                        
                        for tool_call in delta_tool_calls:
                            # Get the index of this tool call (for multiple tools)
                            current_index = None
                            if isinstance(tool_call, dict) and 'index' in tool_call:
                                current_index = tool_call['index']
                            elif hasattr(tool_call, 'index'):
                                current_index = tool_call.index
                            else:
                                current_index = 0
                            
                            # Check if this is a new tool or a continuation
                            if tool_index is None or current_index != tool_index:
                                # New tool call - create a new tool_use block
                                tool_index = current_index
                                last_tool_index += 1
                                anthropic_tool_index = last_tool_index
                                
                                # Extract function info
                                if isinstance(tool_call, dict):
                                    function = tool_call.get('function', {})
                                    name = function.get('name', '') if isinstance(function, dict) else ""
                                    tool_id = tool_call.get('id', f"toolu_{uuid.uuid4().hex[:24]}")
                                else:
                                    function = getattr(tool_call, 'function', None)
                                    name = getattr(function, 'name', '') if function else ''
                                    tool_id = getattr(tool_call, 'id', f"toolu_{uuid.uuid4().hex[:24]}")
                                
                                # Start a new tool_use block
                                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anthropic_tool_index, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {}}})}\n\n"
                                current_tool_call = tool_call
                                tool_content = ""
                            
                            # Extract function arguments
                            arguments = None
                            if isinstance(tool_call, dict) and 'function' in tool_call:
                                function = tool_call.get('function', {})
                                arguments = function.get('arguments', '') if isinstance(function, dict) else ''
                            elif hasattr(tool_call, 'function'):
                                function = getattr(tool_call, 'function', None)
                                arguments = getattr(function, 'arguments', '') if function else ''
                            
                            # If we have arguments, send them as a delta
                            if arguments:
                                # Try to detect if arguments are valid JSON or just a fragment
                                try:
                                    # If it's already a dict, use it
                                    if isinstance(arguments, dict):
                                        args_json = json.dumps(arguments)
                                    else:
                                        # Otherwise, try to parse it
                                        json.loads(arguments)
                                        args_json = arguments
                                except (json.JSONDecodeError, TypeError):
                                    # If it's a fragment, treat it as a string
                                    args_json = arguments
                                
                                # Add to accumulated tool content
                                tool_content += args_json if isinstance(args_json, str) else ""
                                
                                # Send the update
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anthropic_tool_index, 'delta': {'type': 'input_json_delta', 'partial_json': args_json}})}\n\n"
                    
                    # Process finish_reason - end the streaming response
                    if finish_reason and not has_sent_stop_reason:
                        has_sent_stop_reason = True
                        
                        # Close any open tool call blocks
                        if tool_index is not None:
                            for i in range(1, last_tool_index + 1):
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"
                        
                        # If we accumulated text but never sent or closed text block, do it now
                        if not text_block_closed:
                            if accumulated_text and not text_sent:
                                # Send the accumulated text
                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': accumulated_text}})}\n\n"
                            # Close the text block
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
                        
                        # Map OpenAI finish_reason to Anthropic stop_reason
                        stop_reason = "end_turn"
                        if finish_reason == "length":
                            stop_reason = "max_tokens"
                        elif finish_reason == "tool_calls":
                            stop_reason = "tool_use"
                        elif finish_reason == "stop":
                            stop_reason = "end_turn"
                        
                        # Send message_delta with stop reason and usage
                        usage = {"output_tokens": output_tokens}
                        
                        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': usage})}\n\n"
                        
                        # Send message_stop event
                        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                        
                        # Send final [DONE] marker to match Anthropic's behavior
                        yield "data: [DONE]\n\n"
                        return
            except Exception as e:
                # Log error but continue processing other chunks
                logger.error(f"Error processing chunk: {str(e)}")
                continue
        
        # If we didn't get a finish reason, close any open blocks
        if not has_sent_stop_reason:
            # Close any open tool call blocks
            if tool_index is not None:
                for i in range(1, last_tool_index + 1):
                    yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"
            
            # Close the text content block
            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
            
            # Send final message_delta with usage
            usage = {"output_tokens": output_tokens}
            
            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': usage})}\n\n"
            
            # Send message_stop event
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
            
            # Send final [DONE] marker to match Anthropic's behavior
            yield "data: [DONE]\n\n"
    
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        error_message = f"Error in streaming: {str(e)}\n\nFull traceback:\n{error_traceback}"
        logger.error(error_message)
        
        # Send error message_delta
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'error', 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n"
        
        # Send message_stop event
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
        
        # Send final [DONE] marker
        yield "data: [DONE]\n\n"

@app.post("/v1/messages")
async def create_message(
    request: MessagesRequest,
    raw_request: Request
):
    try:
        # print the body here
        body = await raw_request.body()
    
        # Parse the raw body as JSON since it's bytes
        body_json = json.loads(body.decode('utf-8'))
        original_model = body_json.get("model", "unknown")
        request.original_model = original_model
        apply_context_window_route(request, raw_request.headers.get("anthropic-beta", ""))
        
        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]
        
        # Clean model name for capability check
        clean_model = request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/"):]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/"):]
        elif clean_model.startswith("azure/"):
            clean_model = clean_model[len("azure/"):]
        elif clean_model.startswith("azure_ai/"):
            clean_model = clean_model[len("azure_ai/"):]
        
        gated = ccp_lifecycle.manager.not_deployed_response(clean_model)
        if gated is not None:
            return gated

        logger.debug(f"📊 PROCESSING REQUEST: Model={request.model}, Stream={request.stream}")
        
        # Convert Anthropic request to LiteLLM format
        litellm_request = convert_anthropic_to_litellm(request)
        
        # Determine which API key to use based on the model
        if request.model.startswith("azure_ai/"):
            litellm_request["api_key"] = AZURE_AI_API_KEY
            litellm_request["api_base"] = AZURE_AI_API_BASE
            litellm_request["api_version"] = AZURE_AI_API_VERSION
            litellm_request["custom_llm_provider"] = "azure_ai"
            logger.debug(f"Using Azure AI Foundry with base={AZURE_AI_API_BASE} for model: {request.model}")
        elif request.model.startswith("azure/"):
            litellm_request["api_key"] = AZURE_API_KEY
            litellm_request["api_base"] = AZURE_API_BASE
            litellm_request["api_version"] = AZURE_API_VERSION
            logger.debug(f"Using Azure OpenAI with base={AZURE_API_BASE} for model: {request.model}")
        elif request.model.startswith("openai/"):
            litellm_request["api_key"] = OPENAI_API_KEY
            # Use custom OpenAI base URL if configured
            if OPENAI_BASE_URL:
                litellm_request["api_base"] = OPENAI_BASE_URL
                logger.debug(f"Using OpenAI API key and custom base URL {OPENAI_BASE_URL} for model: {request.model}")
            else:
                logger.debug(f"Using OpenAI API key for model: {request.model}")
        elif request.model.startswith("gemini/"):
            if USE_VERTEX_AUTH:
                litellm_request["vertex_project"] = VERTEX_PROJECT
                litellm_request["vertex_location"] = VERTEX_LOCATION
                litellm_request["custom_llm_provider"] = "vertex_ai"
                logger.debug(f"Using Gemini ADC with project={VERTEX_PROJECT}, location={VERTEX_LOCATION} and model: {request.model}")
            else:
                litellm_request["api_key"] = GEMINI_API_KEY
                logger.debug(f"Using Gemini API key for model: {request.model}")
        else:
            # Resolve Anthropic key: env var > client-sent header > fallback
            client_key = getattr(raw_request.state, "client_api_key", None)
            effective_anthropic_key = ANTHROPIC_API_KEY or client_key

            if effective_anthropic_key:
                litellm_request["api_key"] = effective_anthropic_key
                source = "env" if ANTHROPIC_API_KEY else "client header"
                logger.debug(f"Using Anthropic API key ({source}) for model: {request.model}")
            # If no Anthropic key at all but Azure credentials exist, fall back to the configured Azure provider.
            elif PREFERRED_PROVIDER == "azure_ai" and AZURE_AI_API_KEY:
                role = infer_claude_role(request.model)
                litellm_request["model"] = map_role_to_provider_model(role or "big", "azure_ai")
                litellm_request["api_key"] = AZURE_AI_API_KEY
                litellm_request["api_base"] = AZURE_AI_API_BASE
                litellm_request["api_version"] = AZURE_AI_API_VERSION
                litellm_request["custom_llm_provider"] = "azure_ai"
                logger.warning(f"No Anthropic key found; falling back to Azure AI Foundry for model: {request.model}")
            elif AZURE_API_KEY:
                # Try to map Anthropic model names to Azure deployments when possible
                role = infer_claude_role(request.model)
                if role:
                    litellm_request["model"] = map_role_to_provider_model(role, "azure")
                # Set Azure credentials
                litellm_request["api_key"] = AZURE_API_KEY
                litellm_request["api_base"] = AZURE_API_BASE
                litellm_request["api_version"] = AZURE_API_VERSION
                logger.warning(f"No Anthropic key found; falling back to Azure OpenAI for model: {request.model}")
            else:
                logger.error("Missing Anthropic API key and no Azure/OpenAI credentials found")
                raise HTTPException(status_code=500, detail="Missing Anthropic API key")

        group_error = apply_group_route(litellm_request, request.model)
        if group_error is not None:
            return group_error

        # For OpenAI/Azure models - modify request format to work with limitations
        if ("openai" in litellm_request["model"] or litellm_request["model"].startswith("azure/") or litellm_request["model"].startswith("azure_ai/")) and "messages" in litellm_request:
            logger.debug(f"Processing OpenAI model request: {litellm_request['model']}")
            
            # For OpenAI models, we need to convert content blocks to simple strings
            # and handle other requirements
            for i, msg in enumerate(litellm_request["messages"]):
                # Special case - handle message content directly when it's a list of tool_result
                # This is a specific case we're seeing in the error
                if "content" in msg and isinstance(msg["content"], list):
                    is_only_tool_result = True
                    for block in msg["content"]:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            is_only_tool_result = False
                            break
                    
                    if is_only_tool_result and len(msg["content"]) > 0:
                        logger.warning(f"Found message with only tool_result content - special handling required")
                        # Extract the content from all tool_result blocks
                        all_text = ""
                        for block in msg["content"]:
                            all_text += "Tool Result:\n"
                            result_content = block.get("content", [])
                            
                            # Handle different formats of content
                            if isinstance(result_content, list):
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        all_text += item.get("text", "") + "\n"
                                    elif isinstance(item, dict):
                                        # Fall back to string representation of any dict
                                        try:
                                            item_text = item.get("text", json.dumps(item))
                                            all_text += item_text + "\n"
                                        except:
                                            all_text += str(item) + "\n"
                            elif isinstance(result_content, str):
                                all_text += result_content + "\n"
                            else:
                                try:
                                    all_text += json.dumps(result_content) + "\n"
                                except:
                                    all_text += str(result_content) + "\n"
                        
                        # Replace the list with extracted text
                        litellm_request["messages"][i]["content"] = all_text.strip() or "..."
                        logger.warning(f"Converted tool_result to plain text: {all_text.strip()[:200]}...")
                        continue  # Skip normal processing for this message
                
                # 1. Handle content field - normal case
                if "content" in msg:
                    # Check if content is a list (content blocks)
                    if isinstance(msg["content"], list):
                        # Convert complex content blocks to simple string
                        text_content = ""
                        for block in msg["content"]:
                            if isinstance(block, dict):
                                # Handle different content block types
                                if block.get("type") == "text":
                                    text_content += block.get("text", "") + "\n"
                                
                                # Handle tool_result content blocks - extract nested text
                                elif block.get("type") == "tool_result":
                                    tool_id = block.get("tool_use_id", "unknown")
                                    text_content += f"[Tool Result ID: {tool_id}]\n"
                                    
                                    # Extract text from the tool_result content
                                    result_content = block.get("content", [])
                                    if isinstance(result_content, list):
                                        for item in result_content:
                                            if isinstance(item, dict) and item.get("type") == "text":
                                                text_content += item.get("text", "") + "\n"
                                            elif isinstance(item, dict):
                                                # Handle any dict by trying to extract text or convert to JSON
                                                if "text" in item:
                                                    text_content += item.get("text", "") + "\n"
                                                else:
                                                    try:
                                                        text_content += json.dumps(item) + "\n"
                                                    except:
                                                        text_content += str(item) + "\n"
                                    elif isinstance(result_content, dict):
                                        # Handle dictionary content
                                        if result_content.get("type") == "text":
                                            text_content += result_content.get("text", "") + "\n"
                                        else:
                                            try:
                                                text_content += json.dumps(result_content) + "\n"
                                            except:
                                                text_content += str(result_content) + "\n"
                                    elif isinstance(result_content, str):
                                        text_content += result_content + "\n"
                                    else:
                                        try:
                                            text_content += json.dumps(result_content) + "\n"
                                        except:
                                            text_content += str(result_content) + "\n"
                                
                                # Handle tool_use content blocks
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    tool_id = block.get("id", "unknown")
                                    tool_input = json.dumps(block.get("input", {}))
                                    text_content += f"[Tool: {tool_name} (ID: {tool_id})]\nInput: {tool_input}\n\n"
                                
                                # Handle image content blocks
                                elif block.get("type") == "image":
                                    text_content += "[Image content - not displayed in text format]\n"
                        
                        # Make sure content is never empty for OpenAI models
                        if not text_content.strip():
                            text_content = "..."
                        
                        litellm_request["messages"][i]["content"] = text_content.strip()
                    # Also check for None or empty string content
                    elif msg["content"] is None:
                        litellm_request["messages"][i]["content"] = "..." # Empty content not allowed
                
                # 2. Remove any fields OpenAI doesn't support in messages
                for key in list(msg.keys()):
                    if key not in ["role", "content", "name", "tool_call_id", "tool_calls"]:
                        logger.warning(f"Removing unsupported field from message: {key}")
                        del msg[key]
            
            # 3. Final validation - check for any remaining invalid values and dump full message details
            # Azure requires every assistant tool_call to be followed by a
            # matching role=tool message. Claude Code can omit a result when
            # resuming/replaying a session, so repair the provider payload at
            # the final boundary instead of returning a 400 to the client.
            repaired_messages = []
            for msg in litellm_request["messages"]:
                repaired_messages.append(msg)
                if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                    continue
                expected_ids = [call.get("id") for call in msg["tool_calls"] if call.get("id")]
                next_index = len(repaired_messages)
                following_tools = []
                while next_index < len(litellm_request["messages"]):
                    candidate = litellm_request["messages"][next_index]
                    if candidate.get("role") != "tool":
                        break
                    following_tools.append(candidate)
                    next_index += 1
                present_ids = {tool.get("tool_call_id") for tool in following_tools}
                for tool_id in expected_ids:
                    if tool_id not in present_ids:
                        logger.warning("Missing tool result for %s; injecting an empty provider tool response", tool_id)
                        repaired_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "content": "Tool result unavailable; continue without it.",
                        })
            litellm_request["messages"] = repaired_messages

            for i, msg in enumerate(litellm_request["messages"]):
                # Log the message format for debugging
                logger.debug(f"Message {i} format check - role: {msg.get('role')}, content type: {type(msg.get('content'))}")
                
                # If content is still a list or None, replace with placeholder
                if isinstance(msg.get("content"), list):
                    logger.warning(f"CRITICAL: Message {i} still has list content after processing: {json.dumps(msg.get('content'))}")
                    # Last resort - stringify the entire content as JSON
                    litellm_request["messages"][i]["content"] = f"Content as JSON: {json.dumps(msg.get('content'))}"
                elif msg.get("content") is None:
                    logger.warning(f"Message {i} has None content - replacing with placeholder")
                    litellm_request["messages"][i]["content"] = "..." # Fallback placeholder
                elif isinstance(msg.get("content"), str) and not msg["content"].strip():
                    logger.warning(f"Message {i} has empty text content - replacing with placeholder")
                    litellm_request["messages"][i]["content"] = "..."

            for msg in litellm_request["messages"]:
                if isinstance(msg.get("content"), list):
                    nonempty = [
                        block for block in msg["content"]
                        if not (isinstance(block, dict) and block.get("type") == "text" and not str(block.get("text", "")).strip())
                    ]
                    msg["content"] = nonempty or "..."
        
        prepare_provider_request(litellm_request, request)

        # Only log basic info about the request, not the full details
        logger.debug(f"Request for model: {litellm_request.get('model')}, stream: {litellm_request.get('stream', False)}")
        
        # Handle streaming mode
        retry_with_azure = False
        if request.stream:
            # Use LiteLLM for streaming
            num_tools = len(request.tools) if request.tools else 0
            
            log_request_beautifully(
                "POST", 
                raw_request.url.path, 
                display_model, 
                litellm_request.get('model'),
                len(litellm_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            # Ensure we use the async version for streaming
            try:
                response_generator = await litellm.acompletion(**litellm_request)
            except Exception as e:
                # Detect Anthropic 404 / provider not found errors and attempt Azure fallback once
                err_str = str(e)
                if is_provider_retryable_error(err_str) and AZURE_API_KEY and not retry_with_azure:
                    logger.warning("Anthropic provider returned NotFound; attempting Azure OpenAI fallback for streaming request")
                    # Apply Azure fallback mapping
                    def _apply_azure_fallback():
                        role = infer_claude_role(original_model) or "big"
                        if "rate" in err_str.lower() or "429" in err_str:
                            litellm_request["model"] = alternate_azure_model(litellm_request["model"], role)
                            litellm_request["api_key"] = AZURE_API_KEY
                            litellm_request["api_base"] = AZURE_API_BASE
                            litellm_request["api_version"] = AZURE_API_VERSION
                            litellm_request.pop("custom_llm_provider", None)
                        elif PREFERRED_PROVIDER == "azure_ai" and AZURE_AI_API_KEY:
                            litellm_request["model"] = map_role_to_provider_model(role, "azure_ai")
                            litellm_request["api_key"] = AZURE_AI_API_KEY
                            litellm_request["api_base"] = AZURE_AI_API_BASE
                            litellm_request["api_version"] = AZURE_AI_API_VERSION
                            litellm_request["custom_llm_provider"] = "azure_ai"
                        else:
                            litellm_request["model"] = map_role_to_provider_model(role, "azure")
                            litellm_request["api_key"] = AZURE_API_KEY
                            litellm_request["api_base"] = AZURE_API_BASE
                            litellm_request["api_version"] = AZURE_API_VERSION
                    _apply_azure_fallback()
                    retry_with_azure = True

                    # Retry once
                    prepare_provider_request(litellm_request, request)
                    response_generator = await litellm.acompletion(**litellm_request)
                else:
                    raise
            
            return StreamingResponse(
                handle_streaming(response_generator, request),
                media_type="text/event-stream"
            )
        else:
            # Use LiteLLM for regular completion
            num_tools = len(request.tools) if request.tools else 0
            
            log_request_beautifully(
                "POST", 
                raw_request.url.path, 
                display_model, 
                litellm_request.get('model'),
                len(litellm_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            start_time = time.time()
            try:
                litellm_response = litellm.completion(**litellm_request)
            except Exception as e:
                # Inspect error to see if it's an Anthropic model not found - then try Azure fallback once
                err_str = str(e)
                if is_provider_retryable_error(err_str) and AZURE_API_KEY and not retry_with_azure:
                    logger.warning("Anthropic provider returned NotFound; attempting Azure OpenAI fallback for sync request")
                    # Apply Azure fallback mapping
                    role = infer_claude_role(original_model) or "big"
                    if "rate" in err_str.lower() or "429" in err_str:
                        litellm_request["model"] = alternate_azure_model(litellm_request["model"], role)
                        litellm_request["api_key"] = AZURE_API_KEY
                        litellm_request["api_base"] = AZURE_API_BASE
                        litellm_request["api_version"] = AZURE_API_VERSION
                        litellm_request.pop("custom_llm_provider", None)
                    elif PREFERRED_PROVIDER == "azure_ai" and AZURE_AI_API_KEY:
                        litellm_request["model"] = map_role_to_provider_model(role, "azure_ai")
                        litellm_request["api_key"] = AZURE_AI_API_KEY
                        litellm_request["api_base"] = AZURE_AI_API_BASE
                        litellm_request["api_version"] = AZURE_AI_API_VERSION
                        litellm_request["custom_llm_provider"] = "azure_ai"
                    else:
                        litellm_request["model"] = map_role_to_provider_model(role, "azure")
                        litellm_request["api_key"] = AZURE_API_KEY
                        litellm_request["api_base"] = AZURE_API_BASE
                        litellm_request["api_version"] = AZURE_API_VERSION
                    retry_with_azure = True
                    # Retry once
                    prepare_provider_request(litellm_request, request)
                    litellm_response = litellm.completion(**litellm_request)
                else:
                    raise
            logger.debug(f"✅ RESPONSE RECEIVED: Model={litellm_request.get('model')}, Time={time.time() - start_time:.2f}s")
            
            # Convert LiteLLM response to Anthropic format
            anthropic_response = convert_litellm_to_anthropic(litellm_response, request)
            
            return anthropic_response
                
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        
        # Capture as much info as possible about the error
        error_details = {
            "error": str(e),
            "type": type(e).__name__,
            "traceback": error_traceback
        }
        
        # Check for LiteLLM-specific attributes
        for attr in ['message', 'status_code', 'response', 'llm_provider', 'model']:
            if hasattr(e, attr):
                error_details[attr] = getattr(e, attr)
        
        # Check for additional exception details in dictionaries
        if hasattr(e, '__dict__'):
            for key, value in e.__dict__.items():
                if key not in error_details and key not in ['args', '__traceback__']:
                    error_details[key] = str(value)
        
        # Helper function to safely serialize objects for JSON
        def sanitize_for_json(obj):
            """递归地清理对象使其可以JSON序列化"""
            if isinstance(obj, dict):
                return {k: sanitize_for_json(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize_for_json(item) for item in obj]
            elif hasattr(obj, '__dict__'):
                return sanitize_for_json(obj.__dict__)
            elif hasattr(obj, 'text'):
                return str(obj.text)
            else:
                try:
                    json.dumps(obj)
                    return obj
                except (TypeError, ValueError):
                    return str(obj)
        
        # Log all error details with safe serialization
        sanitized_details = sanitize_for_json(error_details)
        logger.error(f"Error processing request: {json.dumps(sanitized_details, indent=2)}")
        
        # Format error for response
        error_message = f"Error: {str(e)}"
        if 'message' in error_details and error_details['message']:
            error_message += f"\nMessage: {error_details['message']}"
        if 'response' in error_details and error_details['response']:
            error_message += f"\nResponse: {error_details['response']}"
        
        # Return detailed error
        status_code = error_details.get('status_code', 500)
        raise HTTPException(status_code=status_code, detail=error_message)

async def stream_openai_chunks(response_generator):
    """Serialize LiteLLM chunks using the OpenAI SSE wire format."""
    async for chunk in response_generator:
        yield f"data: {json.dumps(openai_response_dict(chunk), separators=(',', ':'))}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def create_openai_chat_completion(raw_request: Request):
    """OpenAI-compatible chat completions endpoint backed by CCP's LiteLLM routing."""
    try:
        payload = await raw_request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    original_model = payload.get("model")
    if not isinstance(original_model, str) or not original_model.strip():
        raise HTTPException(status_code=400, detail="model is required")
    if not isinstance(payload.get("messages"), list):
        raise HTTPException(status_code=400, detail="messages must be an array")

    payload = dict(payload)
    for server_owned_key in ("api_key", "api_base", "api_version", "custom_llm_provider"):
        payload.pop(server_owned_key, None)

    requested_effort = payload.get("reasoning_effort")
    payload["model"] = route_openai_compatible_model(original_model, requested_effort)
    gated = ccp_lifecycle.manager.not_deployed_response(payload["model"])
    if gated is not None:
        return gated
    configure_provider_credentials(payload, getattr(raw_request.state, "client_api_key", None))
    group_error = apply_group_route(payload, payload["model"])
    if group_error is not None:
        return group_error
    context = ProviderRequestContext(payload["model"], original_model, requested_effort)
    prepare_provider_request(payload, context)

    try:
        if payload.get("stream", False):
            response_generator = await litellm.acompletion(**payload)
            return StreamingResponse(
                stream_openai_chunks(response_generator),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        response = litellm.completion(**payload)
        return JSONResponse(content=openai_response_dict(response))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("OpenAI-compatible completion failed")
        status_code = getattr(exc, "status_code", 500)
        if not isinstance(status_code, int) or status_code < 400 or status_code > 599:
            status_code = 500
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "message": str(exc),
                    "type": type(exc).__name__,
                    "param": None,
                    "code": None,
                }
            },
        )


@app.get("/models")
@app.get("/v1/models")
async def openai_models():
    """Return configured model IDs using the OpenAI model-list schema."""
    model_ids = list(dict.fromkeys((SMALL_MODEL, BIG_MODEL, FRONTIER_MODEL)))
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": created, "owned_by": PREFERRED_PROVIDER}
            for model_id in model_ids
        ],
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: TokenCountRequest,
    raw_request: Request
):
    try:
        request.original_model = (await raw_request.json()).get("model", request.model)
        apply_context_window_route(request, raw_request.headers.get("anthropic-beta", ""))
        # Log the incoming token count request
        original_model = request.original_model or request.model
        
        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]
        
        # Clean model name for capability check
        clean_model = request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/"):]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/"):]
        elif clean_model.startswith("azure/"):
            clean_model = clean_model[len("azure/"):]
        elif clean_model.startswith("azure_ai/"):
            clean_model = clean_model[len("azure_ai/"):]
        
        # Convert the messages to a format LiteLLM can understand
        converted_request = convert_anthropic_to_litellm(
            MessagesRequest(
                model=request.model,
                max_tokens=100,  # Arbitrary value not used for token counting
                messages=request.messages,
                system=request.system,
                tools=request.tools,
                tool_choice=request.tool_choice,
                thinking=request.thinking
            )
        )
        
        # Use LiteLLM's token_counter function
        try:
            # Import token_counter function
            from litellm import token_counter
            
            # Log the request beautifully
            num_tools = len(request.tools) if request.tools else 0
            
            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                converted_request.get('model'),
                len(converted_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            
            # Prepare token counter arguments
            token_counter_args = {
                "model": converted_request["model"],
                "messages": converted_request["messages"],
            }
            
            # Add custom base URL for OpenAI models if configured
            if request.model.startswith("openai/") and OPENAI_BASE_URL:
                token_counter_args["api_base"] = OPENAI_BASE_URL
            
            # Count tokens
            token_count = token_counter(**token_counter_args)
            
            # Return Anthropic-style response
            return TokenCountResponse(input_tokens=token_count)
            
        except ImportError:
            logger.error("Could not import token_counter from litellm")
            # Fallback to a simple approximation
            return TokenCountResponse(input_tokens=1000)  # Default fallback
            
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        logger.error(f"Error counting tokens: {str(e)}\n{error_traceback}")
        raise HTTPException(status_code=500, detail=f"Error counting tokens: {str(e)}")

@app.get("/ccp/status")
async def ccp_status():
    """Operational status and active model-routing configuration for UI/CLI clients."""
    return {
        "service": "claude-code-proxy",
        "profile": {
            "active": PREPARED_PROFILE.get("active"),
            "label": PREPARED_PROFILE.get("label"),
            "profiles_file": PREPARED_PROFILE.get("profiles_file"),
            "loaded": PREPARED_PROFILE.get("loaded"),
            "provider_instances": PREPARED_PROFILE.get("provider_instances", {}),
        },
        "provider": PREFERRED_PROVIDER,
        "routes": current_model_routes(),
        "providers": PROVIDER_CAPABILITIES,
        "azure": {
            "api_base": AZURE_API_BASE,
            "api_version": AZURE_API_VERSION,
            "subscription_id": AZURE_SUBSCRIPTION_ID,
            "resource_group": AZURE_RESOURCE_GROUP,
            "account_name": AZURE_ACCOUNT_NAME,
            "location": AZURE_LOCATION,
            "configured": bool(AZURE_API_KEY and AZURE_API_BASE),
            "deployment_map_size": len(AZURE_DEPLOYMENT_MAP),
        },
        "azure_ai": {
            "api_base": AZURE_AI_API_BASE,
            "api_version": AZURE_AI_API_VERSION,
            "subscription_id": AZURE_AI_SUBSCRIPTION_ID,
            "resource_group": AZURE_AI_RESOURCE_GROUP,
            "account_name": AZURE_AI_ACCOUNT_NAME,
            "location": AZURE_AI_LOCATION,
            "configured": bool(AZURE_AI_API_KEY and AZURE_AI_API_BASE),
        },
        "proxy_auth_required": bool(PROXY_API_KEY),
    }

@app.get("/ccp/profiles")
async def ccp_profiles():
    """Sanitized profile/account/instance inventory for UI and CLI clients."""
    return public_profiles(PROFILE_CONFIG, os.environ)

@app.get("/ccp/azure/discovery")
async def ccp_azure_discovery(include_deployments: bool = False):
    """Discover Azure subscriptions and Cognitive Services/OpenAI resources via az CLI."""
    try:
        return azure_discovery(include_deployments=include_deployments)
    except AzureDiscoveryError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@app.get("/ccp/models")
async def ccp_models():
    """Static and configured model catalog used by CCP mapping decisions."""
    return {
        "roles": current_model_routes(),
        "providers": {
            "openai": OPENAI_MODELS,
            "gemini": GEMINI_MODELS,
            "azure": AZURE_MODELS,
            "azure_ai": AZURE_MODELS,
        },
        "azure_deployment_map": AZURE_DEPLOYMENT_MAP,
    }

@app.on_event("startup")
async def ccp_start_idle_reaper():
    if os.environ.get("CCP_REAPER", "1") != "0":
        ccp_lifecycle.manager.start_reaper()
    if os.environ.get("CCP_RECONCILE", "1") != "0":
        ccp_lifecycle.manager.schedule_reconcile()


@app.post("/ccp/reconcile")
async def ccp_reconcile(wait: bool = False):
    mgr = ccp_lifecycle.manager
    if wait:
        return {"status": "reconciled", "groups": await mgr.reconcile()}
    mgr.schedule_reconcile()
    return JSONResponse(status_code=202, content={"status": "reconciling"})


@app.get("/ccp/health")
async def ccp_health():
    return {"ok": True}


@app.get("/ccp/active")
async def ccp_active():
    return {
        "big": MODEL_ROLE_CONFIG["big"]["configured_model"],
        "small": MODEL_ROLE_CONFIG["small"]["configured_model"],
        "frontier": MODEL_ROLE_CONFIG["frontier"]["configured_model"],
        "recent": ccp_lifecycle.manager.recent(),
    }


@app.get("/ccp/groups")
async def ccp_groups():
    return ccp_lifecycle.manager.list_groups()


async def _ccp_group_op(op: str, group_id: str, raw_request: Request):
    mgr = ccp_lifecycle.manager
    group = mgr.groups().get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail=f"Unknown group {group_id}")
    try:
        body = await raw_request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    models = body.get("models") if isinstance(body, dict) else None
    if models is not None and (not isinstance(models, list) or not all(isinstance(m, str) for m in models)):
        raise HTTPException(status_code=400, detail="models must be a list of strings")
    if models is not None and not models:
        # explicit empty selection must never widen to "all models"; omit the field for all
        raise HTTPException(status_code=400, detail="models is empty; omit the field to target all models")
    unknown = mgr.unknown_models(group, models)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown models for group {group_id}: {unknown}")
    if mgr.busy(group_id):
        raise HTTPException(status_code=409, detail=f"Group {group_id} already has an operation in progress")
    mgr.schedule(op, group_id, models or None)
    status = "tearing_down" if op == "teardown" else "deploying"
    return JSONResponse(status_code=202, content={"group": group_id, "status": status})


@app.post("/ccp/groups/{group_id}/deploy")
async def ccp_group_deploy(group_id: str, raw_request: Request):
    return await _ccp_group_op("deploy", group_id, raw_request)


@app.post("/ccp/groups/{group_id}/teardown")
async def ccp_group_teardown(group_id: str, raw_request: Request):
    return await _ccp_group_op("teardown", group_id, raw_request)


@app.post("/ccp/groups/{group_id}/redeploy")
async def ccp_group_redeploy(group_id: str, raw_request: Request):
    return await _ccp_group_op("redeploy", group_id, raw_request)


@app.get("/")
async def root():
    return {
        "message": "Anthropic Proxy for LiteLLM",
        "status": "/ccp/status",
        "models": "/ccp/models",
        "profiles": "/ccp/profiles",
        "azure_discovery": "/ccp/azure/discovery",
        "anthropic_messages": "/v1/messages",
        "openai_chat_completions": "/v1/chat/completions",
        "openai_models": "/v1/models",
        "token_count": "/v1/messages/count_tokens",
    }

# Define ANSI color codes for terminal output
class Colors:
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"
def log_request_beautifully(method, path, claude_model, openai_model, num_messages, num_tools, status_code):
    """Log requests in a beautiful, twitter-friendly format showing Claude to OpenAI mapping."""
    # Format the Claude model name nicely
    claude_display = f"{Colors.CYAN}{claude_model}{Colors.RESET}"
    
    # Extract endpoint name
    endpoint = path
    if "?" in endpoint:
        endpoint = endpoint.split("?")[0]
    
    # Extract just the OpenAI model name without provider prefix
    openai_display = openai_model
    if "/" in openai_display:
        openai_display = openai_display.split("/")[-1]
    openai_display = f"{Colors.GREEN}{openai_display}{Colors.RESET}"
    
    # Format tools and messages
    tools_str = f"{Colors.MAGENTA}{num_tools} tools{Colors.RESET}"
    messages_str = f"{Colors.BLUE}{num_messages} messages{Colors.RESET}"
    
    # Format status code
    status_str = f"{Colors.GREEN}✓ {status_code} OK{Colors.RESET}" if status_code == 200 else f"{Colors.RED}✗ {status_code}{Colors.RESET}"
    

    # Put it all together in a clear, beautiful format
    log_line = f"{Colors.BOLD}{method} {endpoint}{Colors.RESET} {status_str}"
    model_line = f"{claude_display} → {openai_display} {tools_str} {messages_str}"
    
    # Print to console
    print(log_line)
    print(model_line)
    sys.stdout.flush()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("Run with: uvicorn server:app --reload --host 0.0.0.0 --port 8082")
        sys.exit(0)
    
    # Configure uvicorn to run with minimal logs
    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="error")
