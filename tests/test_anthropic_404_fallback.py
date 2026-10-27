import os
import importlib
import sys
from fastapi.testclient import TestClient

# Set environment: simulate Anthropic present but returning 404, Azure available
os.environ["ANTHROPIC_API_KEY"] = "invalid-anthropic-key"
os.environ["AZURE_API_KEY"] = "test-azure-key"
os.environ["AZURE_API_BASE"] = "https://azure.test"
os.environ["AZURE_API_VERSION"] = "2025-04-01-preview"

# Ensure repo root is in sys.path so we can import server when running from tests/ folder
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Reload server module so it picks up the environment changes
import server
importlib.reload(server)

# Simulate litellm.completion failing with Anthropic 404 on first attempt, succeeding on second
calls = {"count": 0}
captured = {}

def fake_completion(**kwargs):
    calls["count"] += 1
    # capture the kwargs of the *last* call for assertions
    captured.clear()
    captured.update(kwargs)

    if calls["count"] == 1:
        # Simulate Anthropic 404 style error
        raise Exception("AnthropicException - b'{\"error\":{\"code\":\"404\",\"message\": \"Resource not found\"}}'")
    else:
        return {
            "choices": [
                {"message": {"content": "Hello from fallback litellm"}, "finish_reason": "stop"}
            ],
            "usage": {}
        }

# Monkeypatch litellm completion
server.litellm.completion = fake_completion

client = TestClient(server.app)

response = client.post("/v1/messages", json={
    "model": "anthropic/claude-3-sonnet-20240229",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
})

if response.status_code != 200:
    print("Unexpected status code:", response.status_code)
    print("Body:", response.text)
    sys.exit(1)

# Ensure the service retried and eventually called litellm with Azure credentials
try:
    assert calls["count"] >= 2, "Expected at least one retry"
    assert captured.get("api_key") == os.environ.get("AZURE_API_KEY"), "Expected Azure API key to be used after retry"
    assert captured.get("model", "").startswith("azure/"), f"Expected model to be mapped to an azure deployment, got: {captured.get('model')}"
except AssertionError as e:
    print("Assertion failed:", str(e))
    print("Captured litellm kwargs:", captured)
    sys.exit(1)

print("✅ Anthropic 404 -> Azure fallback test passed")
sys.exit(0)
