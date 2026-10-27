import importlib
import os
import sys

from fastapi.testclient import TestClient

os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["PREFERRED_PROVIDER"] = "azure_ai"
os.environ["AZURE_AI_API_KEY"] = "test-foundry-key"
os.environ["AZURE_AI_API_BASE"] = "https://foundry.test/models"
os.environ["AZURE_AI_API_VERSION"] = "2025-04-01-preview"
os.environ["BIG_MODEL"] = "gpt-5.2-codex"
os.environ["SMALL_MODEL"] = "gpt-5.2-chat"
os.environ["FRONTIER_MODEL"] = "gpt-5.2"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server

importlib.reload(server)

captured = {}


def fake_completion(**kwargs):
    captured.update(kwargs)
    return {
        "choices": [
            {"message": {"content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {},
    }


server.litellm.completion = fake_completion
client = TestClient(server.app)

response = client.post(
    "/v1/messages",
    json={
        "model": "claude-opus-4-7",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Hello"}],
    },
)

assert response.status_code == 200, response.text
assert captured["model"].startswith("azure_ai/"), captured
assert captured["api_key"] == "test-foundry-key", captured
assert captured["api_base"] == "https://foundry.test/models", captured
assert captured["custom_llm_provider"] == "azure_ai", captured

status = client.get("/ccp/status")
assert status.status_code == 200, status.text
status_json = status.json()
assert status_json["provider"] == "azure_ai"
assert status_json["routes"]["frontier"]["routed_model"].startswith("azure_ai/")
assert status_json["providers"]["azure_ai"]["configured"] is True

models = client.get("/ccp/models")
assert models.status_code == 200, models.text
assert "azure_ai" in models.json()["providers"]

print("✅ Azure AI Foundry mapping and status endpoint test passed")
