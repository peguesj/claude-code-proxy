import os
import importlib
import sys
from fastapi.testclient import TestClient

# Ensure env: no Anthropic API key, but Azure is set
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["AZURE_API_KEY"] = "test-azure-key"
os.environ["AZURE_API_BASE"] = "https://azure.test"
os.environ["AZURE_API_VERSION"] = "2025-04-01-preview"

# Ensure repo root is in sys.path so we can import server when running from tests/ folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Reload server module so it picks up the environment changes
import server
importlib.reload(server)

# Capture kwargs passed to litellm.completion
captured = {}

def fake_completion(**kwargs):
    # Capture a shallow copy of the args for assertions
    captured.update(kwargs)
    # Minimal fake response that convert_litellm_to_anthropic can digest
    return {
        "choices": [
            {"message": {"content": "Hello from fake litellm"}, "finish_reason": "stop"}
        ],
        "usage": {}
    }

# Monkeypatch litellm completion
server.litellm.completion = fake_completion

client = TestClient(server.app)

response = client.post("/v1/messages", json={
    "model": "claude-3-sonnet-20240229",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
})

if response.status_code != 200:
    print("Unexpected status code:", response.status_code)
    print("Body:", response.text)
    sys.exit(1)

# Assertions about how the server prepared the Litellm request
try:
    assert captured.get("api_key") == os.environ.get("AZURE_API_KEY"), "Expected Azure API key to be used as api_key"
    assert captured.get("model", "").startswith("azure/"), f"Expected model to be mapped to an azure deployment, got: {captured.get('model')}"
    assert captured.get("api_base") == os.environ.get("AZURE_API_BASE"), "Expected api_base to match AZURE_API_BASE"
except AssertionError as e:
    print("Assertion failed:", str(e))
    print("Captured litellm kwargs:", captured)
    sys.exit(1)

print("✅ Azure fallback unit test passed")
sys.exit(0)
