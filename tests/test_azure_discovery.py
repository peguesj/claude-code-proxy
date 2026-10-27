import os
import sys


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import azure_discovery


def fake_run_az_json(args, timeout=30):
    command = " ".join(args)
    if command == "account show":
        return {
            "id": "sub-a",
            "name": "Sponsorship",
            "tenantId": "tenant-a",
            "state": "Enabled",
            "isDefault": True,
            "user": {"name": "user@example.com"},
        }
    if command == "account list":
        return [
            {
                "id": "sub-a",
                "name": "Sponsorship",
                "tenantId": "tenant-a",
                "state": "Enabled",
                "isDefault": True,
            }
        ]
    if command == "cognitiveservices account list --subscription sub-a":
        return [
            {
                "name": "aoai-prod",
                "resourceGroup": "rg-ai",
                "location": "eastus2",
                "kind": "OpenAI",
                "sku": {"name": "S0"},
                "properties": {"endpoint": "https://aoai-prod.openai.azure.com/"},
            }
        ]
    if command == "cognitiveservices account deployment list --resource-group rg-ai --name aoai-prod --subscription sub-a":
        return [
            {
                "name": "gpt-5-2",
                "sku": {"name": "GlobalStandard", "capacity": 1},
                "properties": {
                    "provisioningState": "Succeeded",
                    "model": {"name": "gpt-5.2", "version": "2026-01-01", "format": "OpenAI"},
                },
            }
        ]
    raise AssertionError(f"Unexpected az args: {args}")


azure_discovery.run_az_json = fake_run_az_json
result = azure_discovery.discovery(include_deployments=True)

assert result["active_account"]["id"] == "sub-a"
assert result["subscriptions"][0]["name"] == "Sponsorship"
account = result["cognitive_accounts"]["sub-a"]["accounts"][0]
assert account["name"] == "aoai-prod"
assert account["deployments"][0]["model"] == "gpt-5.2"

print("✅ Azure discovery module test passed")
