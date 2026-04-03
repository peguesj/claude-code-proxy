import os
import importlib
import sys

# Set env: Azure provider with frontier model
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["PREFERRED_PROVIDER"] = "azure"
os.environ["AZURE_API_KEY"] = "test-azure-key"
os.environ["AZURE_API_BASE"] = "https://azure.test"
os.environ["AZURE_API_VERSION"] = "2025-04-01-preview"
os.environ["BIG_MODEL"] = "gpt-4.1"
os.environ["SMALL_MODEL"] = "gpt-4.1-mini"
os.environ["FRONTIER_MODEL"] = "gpt-5.2"

# Ensure repo root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import server
importlib.reload(server)

# Capture kwargs passed to litellm.completion
captured = {}

def fake_completion(**kwargs):
    captured.update(kwargs)
    return {
        "choices": [
            {"message": {"content": "Hello from fake litellm"}, "finish_reason": "stop"}
        ],
        "usage": {}
    }

server.litellm.completion = fake_completion
from fastapi.testclient import TestClient
client = TestClient(server.app)

# --- Test 1: opus model maps to FRONTIER_MODEL via Azure ---
captured.clear()
response = client.post("/v1/messages", json={
    "model": "claude-opus-4-6",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
})

if response.status_code != 200:
    print("Test 1 - Unexpected status code:", response.status_code)
    print("Body:", response.text)
    sys.exit(1)

try:
    assert captured.get("model", "").startswith("azure/"), f"Test 1 - Expected azure/ prefix, got: {captured.get('model')}"
    assert "gpt-5-2" in captured.get("model", "") or "gpt-5.2" in captured.get("model", ""), f"Test 1 - Expected gpt-5.2 deployment, got: {captured.get('model')}"
    print("  Test 1 passed: opus -> azure/gpt-5-2")
except AssertionError as e:
    print("Test 1 - Assertion failed:", str(e))
    print("Captured litellm kwargs:", captured)
    sys.exit(1)

# --- Test 2: sonnet still maps to BIG_MODEL ---
captured.clear()
response = client.post("/v1/messages", json={
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
})

if response.status_code != 200:
    print("Test 2 - Unexpected status code:", response.status_code)
    sys.exit(1)

try:
    assert "gpt-4-1" in captured.get("model", "") or "gpt-4.1" in captured.get("model", ""), f"Test 2 - Expected gpt-4.1 deployment, got: {captured.get('model')}"
    print("  Test 2 passed: sonnet -> azure/gpt-4-1")
except AssertionError as e:
    print("Test 2 - Assertion failed:", str(e))
    sys.exit(1)

# --- Test 3: haiku still maps to SMALL_MODEL ---
captured.clear()
response = client.post("/v1/messages", json={
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}],
})

if response.status_code != 200:
    print("Test 3 - Unexpected status code:", response.status_code)
    sys.exit(1)

try:
    assert "gpt-4-1-mini" in captured.get("model", "") or "gpt-4.1-mini" in captured.get("model", ""), f"Test 3 - Expected gpt-4.1-mini deployment, got: {captured.get('model')}"
    print("  Test 3 passed: haiku -> azure/gpt-4-1-mini")
except AssertionError as e:
    print("Test 3 - Assertion failed:", str(e))
    sys.exit(1)

print("All opus/frontier mapping tests passed")
sys.exit(0)
