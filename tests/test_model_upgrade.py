"""Offline regression tests for Claude effort routing and provider compatibility.

Run with: .venv/bin/python tests/test_model_upgrade.py
"""

import copy
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Never load the user's credentials or selected profile in this test process.
with patch.dict(os.environ, {
    "PREFERRED_PROVIDER": "azure",
    "AZURE_API_KEY": "test-key",
    "AZURE_API_BASE": "https://azure.test",
    "BIG_MODEL": "gpt-5.6-terra",
    "SMALL_MODEL": "gpt-5.6-luna",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}, clear=True), patch("dotenv.load_dotenv"):
    import server

from fastapi.testclient import TestClient


class ModelUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.calls = []
        quiet = patch.object(server, "log_request_beautifully")
        quiet.start()
        self.addCleanup(quiet.stop)

    def completion(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }

    def post(self, model="claude-sonnet-5", *, effort="high", **fields):
        payload = {
            "model": model,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Hello"}],
            **fields,
        }
        if effort is not None:
            payload.setdefault("output_config", {"effort": effort})
        with patch.object(server.litellm, "completion", side_effect=self.completion):
            response = self.client.post("/v1/messages", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_frontier_default_and_existing_routes(self):
        self.assertEqual(server.FRONTIER_MODEL, "gpt-6-astra")
        for model, expected in (
            ("claude-opus-4-6", "azure/responses/gpt-6-astra"),
            ("claude-sonnet-5", "azure/gpt-5.6-terra"),
            ("claude-haiku-4-5", "azure/gpt-5.6-luna"),
        ):
            with self.subTest(model=model):
                self.post(model)
                self.assertEqual(self.calls[-1]["model"], expected)

    def test_claude_2_1_296_frontier_families(self):
        for model in (
            "fable",
            "claude-fable-5-1",
            "mythos",
            "claude-mythos-5-1",
        ):
            with self.subTest(model=model):
                self.post(model)
                self.assertEqual(self.calls[-1]["model"], "azure/responses/gpt-6-astra")
                count = server.TokenCountRequest(model=model, messages=[])
                self.assertEqual(count.model, "azure/gpt-6-astra")

    def test_claude_2_1_296_resume_tool_additions(self):
        self.post("claude-opus-5-5", messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Adding a dynamically discovered tool."},
                    {
                        "type": "tool_addition",
                        "tool": {
                            "name": "dynamic_probe",
                            "description": "A tool discovered after session start.",
                            "input_schema": {"type": "object", "properties": {}},
                        },
                    },
                ],
            },
            {"role": "user", "content": "Continue"},
        ])
        self.assertEqual(self.calls[-1]["model"], "azure/responses/gpt-6-astra")
        self.assertNotIn("tool_addition", json.dumps(self.calls[-1]["messages"]))

    def test_new_model_stays_on_azure_with_an_older_deployment_map(self):
        with patch.object(server, "AZURE_MODELS", ["gpt-4.1"]), patch.dict(server.AZURE_DEPLOYMENT_MAP, {"gpt-4.1": "gpt-4-1"}, clear=True):
            self.post("gpt-6-astra", effort="low")
            self.assertEqual(self.calls[-1]["model"], "azure/responses/gpt-6-astra")
            self.assertEqual(self.calls[-1]["api_key"], "test-key")
            count = server.TokenCountRequest(model="gpt-6-astra", messages=[])
            self.assertEqual(count.model, "azure/gpt-6-astra")

    def test_output_config_effort_is_translated(self):
        for provider in ("azure", "openai", "azure_ai"):
            with self.subTest(provider=provider), patch.object(server, "PREFERRED_PROVIDER", provider):
                self.post(effort="med")
                self.assertNotIn("output_config", self.calls[-1])
                self.assertEqual(self.calls[-1]["reasoning_effort"], "medium")

    def test_nonreasoning_backend_does_not_receive_effort(self):
        with patch.dict(server.MODEL_ROLE_CONFIG["big"], configured_model="gpt-4.1"):
            self.post()
        self.assertNotIn("output_config", self.calls[-1])
        self.assertNotIn("reasoning_effort", self.calls[-1])

    def test_opus_1m_effort_tiers(self):
        for effort, tier in (("low", "luna"), ("medium", "terra"), ("med", "terra"), ("high", "sol"), (None, "terra")):
            for alias in ("opus[1m]", "claude-opus-4-6[1m]", "anthropic/claude-opus-4-6-1m"):
                with self.subTest(effort=effort, alias=alias):
                    self.post(alias, effort=effort)
                    self.assertEqual(self.calls[-1]["model"], f"azure/gpt-5.6-{tier}")
                    self.assertNotIn("output_config", self.calls[-1])

    def test_effort_aliases_and_explicit_override(self):
        for effort, tier in (("low", "luna"), ("medium", "terra"), ("med", "terra"), ("high", "sol")):
            with self.subTest(effort=effort):
                self.post(f"opus[1m]-{effort}", effort=None)
                self.assertEqual(self.calls[-1]["model"], f"azure/gpt-5.6-{tier}")
                self.assertEqual(self.calls[-1]["reasoning_effort"], "medium" if effort == "med" else effort)
        self.post("opus[1m]-low", effort="high")
        self.assertEqual(self.calls[-1]["model"], "azure/gpt-5.6-sol")

    def test_context_beta_header_selects_opus_1m_only(self):
        with patch.object(server.litellm, "completion", side_effect=self.completion):
            for model, expected in (("claude-opus-4-6", "sol"), ("claude-sonnet-5", "terra")):
                response = self.client.post("/v1/messages", headers={"anthropic-beta": "context-1m-2025-08-07"}, json={
                    "model": model, "max_tokens": 128,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "output_config": {"effort": "high"},
                })
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.calls[-1]["model"], f"azure/gpt-5.6-{expected}")

    def test_custom_azure_deployment(self):
        with patch.dict(server.AZURE_DEPLOYMENT_MAP, {"gpt-5.6-luna": "custom-luna"}):
            self.post("opus[1m]", effort="low")
        self.assertEqual(self.calls[-1]["model"], "azure/custom-luna")
        self.assertEqual(self.calls[-1]["reasoning_effort"], "low")

    def test_anthropic_passthrough_preserves_output_config(self):
        with patch.object(server, "PREFERRED_PROVIDER", "anthropic"), patch.object(server, "ANTHROPIC_API_KEY", "test-anthropic"):
            self.post("claude-opus-4-6[1m]")
        self.assertEqual(self.calls[-1]["model"], "anthropic/claude-opus-4-6[1m]")
        self.assertEqual(self.calls[-1]["output_config"], {"effort": "high"})

    def test_tool_choice_without_tools_is_removed(self):
        for provider in ("anthropic", "azure"):
            with self.subTest(provider=provider), patch.object(server, "PREFERRED_PROVIDER", provider), patch.object(server, "ANTHROPIC_API_KEY", "test-anthropic"):
                self.post("claude-sonnet-5", tool_choice={"type": "auto"}, tools=[])
                self.assertNotIn("tool_choice", self.calls[-1])
                self.assertNotIn("tools", self.calls[-1])

    def test_retry_retranslates_anthropic_output_config(self):
        normal_completion = self.completion

        def fail_once(**kwargs):
            if not self.calls:
                self.calls.append(copy.deepcopy(kwargs))
                raise RuntimeError("AnthropicException: Resource not found")
            return normal_completion(**kwargs)

        with patch.object(server, "PREFERRED_PROVIDER", "anthropic"), patch.object(server, "ANTHROPIC_API_KEY", "test-anthropic"), patch.object(self, "completion", side_effect=fail_once):
            self.post("claude-opus-4-6")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1]["model"], "azure/responses/gpt-6-astra")
        self.assertNotIn("output_config", self.calls[-1])

    def test_astra_uses_responses_without_sampling_parameters(self):
        self.post("claude-opus-4-6", temperature=0.4, top_p=0.8, top_k=10)
        for parameter in ("output_config", "temperature", "top_p", "top_k"):
            self.assertNotIn(parameter, self.calls[-1])

    def test_structured_output_is_translated(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
        self.post(output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}})
        self.assertNotIn("output_config", self.calls[-1])
        self.assertEqual(self.calls[-1]["response_format"]["json_schema"]["schema"], schema)

    def test_structured_output_schema_is_strictified(self):
        schema = {
            "type": "object",
            "properties": {
                "verdict": {"type": "string"},
                "impossible": {"type": "boolean"},
                "details": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": [],
                },
            },
            "required": ["verdict"],
        }
        self.post(output_config={"format": {"type": "json_schema", "schema": schema}})
        strict_schema = self.calls[-1]["response_format"]["json_schema"]["schema"]
        self.assertEqual(strict_schema["required"], ["verdict", "impossible", "details"])
        self.assertEqual(strict_schema["properties"]["details"]["required"], ["reason"])
        self.assertFalse(strict_schema["additionalProperties"])
        self.assertFalse(strict_schema["properties"]["details"]["additionalProperties"])
        self.assertEqual(schema["required"], ["verdict"])

    def test_tool_calls_remain_structured_on_non_anthropic_backends(self):
        request = server.MessagesRequest(model="claude-opus-4-6", max_tokens=128, messages=[])
        response = server.convert_litellm_to_anthropic({
            "choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call_probe", "type": "function",
                "function": {"name": "probe", "arguments": '{"value":"ok"}'},
            }]}, "finish_reason": "tool_calls"}],
            "usage": {},
        }, request)
        self.assertEqual(response.content[0].type, "tool_use")
        self.assertEqual(response.content[0].input, {"value": "ok"})
        self.assertEqual(response.stop_reason, "tool_use")

    def test_streaming_requests_translate_output_config(self):
        async def completion(**kwargs):
            self.calls.append(copy.deepcopy(kwargs))

            async def chunks():
                yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}
                yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

            return chunks()

        with patch.object(server.litellm, "acompletion", side_effect=completion):
            response = self.client.post("/v1/messages", json={
                "model": "opus[1m]", "max_tokens": 128, "stream": True,
                "messages": [{"role": "user", "content": "Hello"}],
                "output_config": {"effort": "low"},
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: message_stop", response.text)
        self.assertEqual(self.calls[-1]["model"], "azure/gpt-5.6-luna")
        self.assertNotIn("output_config", self.calls[-1])

    def test_token_count_uses_same_alias_mapping(self):
        request = server.TokenCountRequest(model="opus[1m]-low", messages=[])
        self.assertEqual(request.model, "azure/gpt-5.6-luna")

    def test_astra_real_litellm_responses_transport(self):
        """Exercise the installed LiteLLM bridge, not just mocked completion kwargs."""
        observed = []

        def send(request, **kwargs):
            observed.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, request=request, json={
                "id": "resp_test", "created_at": 1, "object": "response",
                "model": "gpt-6-astra", "status": "completed",
                "output": [{"id": "fc_test", "type": "function_call", "call_id": "call_probe",
                            "name": "probe", "arguments": '{"value":"ok"}', "status": "completed"}],
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                "parallel_tool_calls": True, "tool_choice": "required", "tools": [],
            })

        request = server.MessagesRequest(
            model="gpt-6-astra", max_tokens=128, output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Call probe."}],
            tools=[{"name": "probe", "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}}}],
            tool_choice={"type": "any"},
        )
        payload = server.convert_anthropic_to_litellm(request)
        payload.update(api_key="test-key", api_base="https://azure.test", api_version="2025-04-01-preview")
        server.prepare_provider_request(payload, request)
        with patch.object(httpx.Client, "send", side_effect=send), patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Unexpected async network call")):
            result = server.litellm.completion(**payload)
        self.assertEqual(len(observed), 1)
        url, wire_payload = observed[0]
        self.assertIn("/openai/responses", url)
        self.assertEqual(wire_payload["model"], "gpt-6-astra")
        self.assertEqual(wire_payload["reasoning"], {"effort": "low"})
        self.assertEqual(wire_payload["tools"][0]["name"], "probe")
        self.assertEqual(wire_payload["tool_choice"], "required")
        self.assertNotIn("output_config", wire_payload)
        self.assertNotIn("temperature", wire_payload)
        converted = server.convert_litellm_to_anthropic(result, request)
        self.assertEqual(converted.content[0].type, "tool_use")
        self.assertEqual(converted.content[0].input, {"value": "ok"})

    def test_custom_astra_deployment_and_foundry_endpoint(self):
        with patch.dict(server.AZURE_DEPLOYMENT_MAP, {"gpt-6-astra": "custom-astra"}):
            with patch.object(server, "PREFERRED_PROVIDER", "azure_ai"), patch.object(server, "AZURE_AI_API_BASE", "https://foundry.services.ai.azure.com/models"):
                self.post("claude-opus-4-6")
        self.assertEqual(self.calls[-1]["model"], "azure/responses/custom-astra")
        self.assertEqual(self.calls[-1]["api_base"], "https://foundry.services.ai.azure.com")
        self.assertEqual(self.calls[-1]["custom_llm_provider"], "azure")

    def test_56_tools_with_reasoning_use_responses(self):
        for model, tier in (("claude-sonnet-5", "terra"), ("claude-haiku-4-5", "luna"), ("opus[1m]", "sol")):
            with self.subTest(model=model):
                self.post(model, tools=[{"name": "probe", "input_schema": {"type": "object", "properties": {}}}])
                self.assertEqual(self.calls[-1]["model"], f"azure/responses/gpt-5.6-{tier}")
                self.assertEqual(self.calls[-1]["reasoning_effort"], "high")
                self.assertEqual(self.calls[-1]["custom_llm_provider"], "azure")

    def test_malformed_effort_does_not_crash_routing(self):
        for effort in (42, {}, "unsupported", " HIGH "):
            with self.subTest(effort=effort):
                self.post("opus[1m]", effort=effort)
                self.assertNotIn("output_config", self.calls[-1])

    def test_count_tokens_context_beta_header(self):
        with patch.object(server.litellm, "token_counter", return_value=3) as counter:
            response = self.client.post("/v1/messages/count_tokens", headers={"anthropic-beta": "context-1m-2025-08-07"}, json={
                "model": "claude-opus-4-6", "output_config": {"effort": "low"},
                "messages": [{"role": "user", "content": "Hello"}],
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(counter.call_args.kwargs["model"], "azure/gpt-5.6-luna")

    def test_switcher_and_menu_presets_match(self):
        root = Path(__file__).resolve().parents[1]
        switcher = (root / "ccp-switcher").read_text()

        def array(name):
            block = re.search(rf'declare -a {name}=\(((?:[^"()]|"[^"]*")*)\)', switcher, re.S)
            self.assertIsNotNone(block, name)
            return re.findall(r'"([^"\n]*)"', block.group(1))

        self.assertIn("gpt-6-astra", array("MODEL_NAMES"))
        self.assertEqual(len(array("MODEL_NAMES")), len(array("MODEL_DESCRIPTIONS")))
        cli_presets = list(zip(*(array(name) for name in (
            "PRESET_NAMES", "PRESET_PROVIDERS", "PRESET_BIG", "PRESET_SMALL", "PRESET_FRONTIER",
        ))))
        swift_presets = re.findall(
            r'Preset\(name: "([^"]+)",\s+provider: "([^"]+)",\s+big: "([^"]+)",\s+small: "([^"]+)",\s+frontier: "([^"]+)"\)',
            (root / "CcpMenuBar.swift").read_text(),
        )
        self.assertEqual(cli_presets, swift_presets)

    def test_profile_examples_include_frontier_and_effort_deployments(self):
        path = Path(__file__).resolve().parents[1] / "profiles.example.json"
        profiles = json.loads(path.read_text())["profiles"]
        for profile in profiles.values():
            self.assertEqual(profile["routes"]["frontier"], "gpt-6-astra")
            for provider in ("azure", "azure_ai"):
                for instance in profile.get("providers", {}).get(provider, {}).get("instances", {}).values():
                    for model in ("gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
                        self.assertIn(model, instance.get("deployment_map", server._default_azure_map))


if __name__ == "__main__":
    unittest.main(verbosity=2)
