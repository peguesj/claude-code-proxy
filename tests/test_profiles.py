import importlib
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


with tempfile.TemporaryDirectory() as tmpdir:
    profiles_path = os.path.join(tmpdir, "profiles.json")
    with open(profiles_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "active_profile": "sponsorship",
                "profiles": {
                    "sponsorship": {
                        "label": "Azure Sponsorship",
                        "preferred_provider": "azure_ai",
                        "routes": {
                            "frontier": "gpt-5.2",
                            "big": "gpt-5.2-codex",
                            "small": "gpt-5.2-chat",
                        },
                        "providers": {
                            "azure": {
                                "default": "eastus-openai",
                                "instances": {
                                    "eastus-openai": {
                                        "subscription_id": "sub-openai",
                                        "resource_group": "rg-openai",
                                        "account_name": "aoai-prod",
                                        "api_key_env": "PROFILE_AZURE_KEY",
                                        "api_base": "https://aoai.test.openai.azure.com/",
                                        "api_version": "2025-04-01-preview",
                                        "deployment_map": {
                                            "gpt-5.2": "aoai-gpt-5-2"
                                        },
                                    }
                                },
                            },
                            "azure_ai": {
                                "default": "eus2-foundry",
                                "instances": {
                                    "eus2-foundry": {
                                        "subscription_id": "sub-foundry",
                                        "resource_group": "rg-foundry",
                                        "account_name": "foundry-prod",
                                        "location": "eastus2",
                                        "api_key_env": "PROFILE_FOUNDRY_KEY",
                                        "api_base": "https://foundry.test.services.ai.azure.com/models",
                                        "api_version": "2025-04-01-preview",
                                        "deployment_map": {
                                            "gpt-5.2": "foundry-gpt-5-2",
                                            "gpt-5.2-codex": "foundry-gpt-5-2-codex",
                                            "gpt-5.2-chat": "foundry-gpt-5-2-chat",
                                        },
                                    }
                                },
                            },
                        },
                    }
                },
            },
            handle,
        )

    for key in (
        "PREFERRED_PROVIDER",
        "BIG_MODEL",
        "SMALL_MODEL",
        "FRONTIER_MODEL",
        "AZURE_API_KEY",
        "AZURE_API_BASE",
        "AZURE_AI_API_KEY",
        "AZURE_AI_API_BASE",
        "AZURE_DEPLOYMENT_MAP",
    ):
        os.environ.pop(key, None)

    os.environ["CCP_PROFILES_FILE"] = profiles_path
    os.environ["CCP_PROFILE"] = "sponsorship"
    os.environ["PROFILE_AZURE_KEY"] = "profile-azure-key"
    os.environ["PROFILE_FOUNDRY_KEY"] = "profile-foundry-key"

    import server

    importlib.reload(server)

    assert server.PREPARED_PROFILE["active"] == "sponsorship"
    assert server.PREFERRED_PROVIDER == "azure_ai"
    assert server.AZURE_AI_API_KEY == "profile-foundry-key"
    assert server.AZURE_AI_API_BASE == "https://foundry.test.services.ai.azure.com/models"
    assert server.AZURE_AI_RESOURCE_GROUP == "rg-foundry"
    assert server.AZURE_AI_ACCOUNT_NAME == "foundry-prod"
    assert server.AZURE_DEPLOYMENT_MAP["gpt-5.2"] == "foundry-gpt-5-2"

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
    assert captured["model"] == "azure_ai/foundry-gpt-5-2", captured
    assert captured["api_key"] == "profile-foundry-key", captured
    assert captured["api_base"] == "https://foundry.test.services.ai.azure.com/models", captured

    status = client.get("/ccp/status")
    assert status.status_code == 200, status.text
    status_json = status.json()
    assert status_json["profile"]["active"] == "sponsorship"
    assert status_json["profile"]["provider_instances"]["azure_ai"]["active"] == "eus2-foundry"
    assert status_json["profile"]["provider_instances"]["azure"]["active"] == "eastus-openai"
    assert "profile-foundry-key" not in json.dumps(status_json["profile"])
    assert "profile-azure-key" not in json.dumps(status_json["profile"])

    profiles = client.get("/ccp/profiles")
    assert profiles.status_code == 200, profiles.text
    profiles_json = profiles.json()
    assert profiles_json["active"] == "sponsorship"
    assert profiles_json["profiles"]["sponsorship"]["providers"]["azure_ai"]["active"] == "eus2-foundry"

print("✅ Profile overlay and status endpoint test passed")
