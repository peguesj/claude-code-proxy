import importlib
import json
import os
import sys

from fastapi.testclient import TestClient


os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("PROXY_API_KEY", None)
os.environ["PREFERRED_PROVIDER"] = "azure"
os.environ["AZURE_API_KEY"] = "test-azure-key"
os.environ["AZURE_API_BASE"] = "https://azure.test/"
os.environ["AZURE_API_VERSION"] = "2025-04-01-preview"
os.environ["BIG_MODEL"] = "gpt-5.6-terra"
os.environ["SMALL_MODEL"] = "gpt-5.6-luna"
os.environ["FRONTIER_MODEL"] = "gpt-6-astra"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import server


importlib.reload(server)
client = TestClient(server.app)
captured = {}


def fake_completion(**kwargs):
    captured.update(kwargs)
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5.6-terra",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


server.litellm.completion = fake_completion

response = client.post(
    "/v1/chat/completions",
    json={
        "model": "gpt-5.6-terra",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 32,
    },
)
assert response.status_code == 200, response.text
assert response.json()["object"] == "chat.completion"
assert captured["model"] == "azure/gpt-5.6-terra", captured
assert captured["api_key"] == "test-azure-key", captured
assert captured["api_base"] == "https://azure.test/", captured

# Some clients append /chat/completions themselves instead of /v1/chat/completions.
alias_response = client.post(
    "/chat/completions",
    json={"model": "claude-sonnet", "messages": [{"role": "user", "content": "Hello"}]},
)
assert alias_response.status_code == 200, alias_response.text
assert captured["model"] == "azure/gpt-5.6-terra", captured

models_response = client.get("/v1/models")
assert models_response.status_code == 200, models_response.text
assert {item["id"] for item in models_response.json()["data"]} == {
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-6-astra",
}


async def fake_acompletion(**kwargs):
    captured.update(kwargs)

    async def chunks():
        yield {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
        }

    return chunks()


server.litellm.acompletion = fake_acompletion
stream_response = client.post(
    "/v1/chat/completions",
    json={
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": True,
    },
)
assert stream_response.status_code == 200, stream_response.text
assert "chat.completion.chunk" in stream_response.text
assert stream_response.text.rstrip().endswith("data: [DONE]")

first_data_line = next(line for line in stream_response.text.splitlines() if line.startswith("data: {")).removeprefix("data: ")
assert json.loads(first_data_line)["choices"][0]["delta"]["content"] == "hi"

print("✅ OpenAI-compatible chat completions and model-list endpoints passed")
