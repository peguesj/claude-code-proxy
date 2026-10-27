"""Profile resolution for CCP provider accounts and resource instances.

Profiles are intentionally data-only. Secrets should stay in environment
variables and profiles should reference them with api_key_env.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple


SECRET_MARKERS = ("key", "secret", "token", "password")


def default_profiles_file(env: Mapping[str, str] = os.environ) -> str:
    configured = env.get("CCP_PROFILES_FILE")
    if configured:
        return os.path.expanduser(configured)
    return str(Path(__file__).with_name("profiles.json"))


def load_profiles(path: Optional[str] = None) -> Dict[str, Any]:
    profiles_path = os.path.expanduser(path or default_profiles_file())
    if not os.path.exists(profiles_path):
        return {"profiles": {}, "_profiles_file": profiles_path, "_loaded": False}

    with open(profiles_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(f"Profile file must contain a JSON object: {profiles_path}")

    data.setdefault("profiles", {})
    data["_profiles_file"] = profiles_path
    data["_loaded"] = True
    return data


def active_profile_name(config: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> Optional[str]:
    selected = env.get("CCP_PROFILE") or env.get("ACTIVE_PROFILE") or config.get("active_profile")
    if selected:
        return str(selected)
    profiles = config.get("profiles") or {}
    if len(profiles) == 1:
        return next(iter(profiles.keys()))
    return None


def active_profile(config: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> Tuple[Optional[str], Dict[str, Any]]:
    name = active_profile_name(config, env)
    profiles = config.get("profiles") or {}
    if name and isinstance(profiles.get(name), dict):
        return name, dict(profiles[name])
    return name, {}


def provider_instance(
    profile: Mapping[str, Any],
    provider: str,
    env: Mapping[str, str] = os.environ,
) -> Tuple[Optional[str], Dict[str, Any]]:
    providers = profile.get("providers") or {}
    provider_config = providers.get(provider) or {}
    if not isinstance(provider_config, dict):
        return None, {}

    env_selector = f"CCP_{provider.upper()}_INSTANCE"
    selected = env.get(env_selector) or provider_config.get("active") or provider_config.get("default")
    instances = provider_config.get("instances") or {}

    if selected and isinstance(instances.get(str(selected)), dict):
        return str(selected), dict(instances[str(selected)])
    if len(instances) == 1:
        only_name = next(iter(instances.keys()))
        return str(only_name), dict(instances[only_name])
    return str(selected) if selected else None, {}


def prepare_profile(config: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> Dict[str, Any]:
    name, profile = active_profile(config, env)
    providers = profile.get("providers") if isinstance(profile, dict) else {}
    provider_instances: Dict[str, Any] = {}

    if isinstance(providers, dict):
        for provider_name in providers.keys():
            instance_name, instance = provider_instance(profile, provider_name, env)
            provider_instances[provider_name] = {
                "active": instance_name,
                "configured": bool(instance),
                "settings": sanitize(instance),
            }

    return {
        "active": name,
        "label": profile.get("label") if isinstance(profile, dict) else None,
        "profiles_file": config.get("_profiles_file"),
        "loaded": bool(config.get("_loaded")),
        "profile": profile,
        "provider_instances": provider_instances,
    }


def profile_value(
    prepared: Mapping[str, Any],
    key: str,
    env_key: str,
    default: Optional[str] = None,
    env: Mapping[str, str] = os.environ,
) -> Optional[str]:
    profile = prepared.get("profile") or {}
    if isinstance(profile, dict) and profile.get(key) is not None:
        return str(profile[key])
    return env.get(env_key, default)


def route_value(
    prepared: Mapping[str, Any],
    route: str,
    env_key: str,
    default: str,
    env: Mapping[str, str] = os.environ,
) -> str:
    profile = prepared.get("profile") or {}
    routes = profile.get("routes") if isinstance(profile, dict) else {}
    if isinstance(routes, dict) and routes.get(route):
        return str(routes[route])
    return env.get(env_key, default)


def provider_setting(
    prepared: Mapping[str, Any],
    provider: str,
    field: str,
    env_key: str,
    default: Optional[str] = None,
    env: Mapping[str, str] = os.environ,
) -> Optional[str]:
    instance = active_provider_settings(prepared, provider)
    if field == "api_key":
        keychain_ref = instance.get("api_key_keychain")
        keychain_value = read_keychain_secret(keychain_ref)
        if keychain_value:
            return keychain_value
        key_env = instance.get("api_key_env")
        if key_env and env.get(str(key_env)):
            return env.get(str(key_env))
        if instance.get("api_key"):
            return str(instance["api_key"])
    elif instance.get(field) is not None:
        return str(instance[field])
    return env.get(env_key, default)


def read_keychain_secret(ref: Any) -> Optional[str]:
    """Read a macOS Keychain generic password reference.

    Supported forms:
    - "service/account"
    - {"service": "ccp.azure_ai", "account": "prod-foundry"}
    """
    if not ref:
        return None

    service: Optional[str] = None
    account: Optional[str] = None
    if isinstance(ref, str):
        if "/" in ref:
            service, account = ref.split("/", 1)
        else:
            service = ref
    elif isinstance(ref, dict):
        service = ref.get("service")
        account = ref.get("account")

    if not service:
        return None

    cmd = ["/usr/bin/security", "find-generic-password", "-w", "-s", str(service)]
    if account:
        cmd.extend(["-a", str(account)])

    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def active_provider_settings(prepared: Mapping[str, Any], provider: str) -> Dict[str, Any]:
    profile = prepared.get("profile") or {}
    if not isinstance(profile, dict):
        return {}
    _, instance = provider_instance(profile, provider)
    return instance


def deployment_map(
    prepared: Mapping[str, Any],
    provider: str,
    default_map: Mapping[str, str],
    env_key: str = "AZURE_DEPLOYMENT_MAP",
    env: Mapping[str, str] = os.environ,
) -> Dict[str, str]:
    instance = active_provider_settings(prepared, provider)
    profile_map = instance.get("deployment_map")
    if isinstance(profile_map, dict):
        return {str(key): str(value) for key, value in profile_map.items()}

    custom_map = env.get(env_key, "")
    if custom_map:
        try:
            parsed = json.loads(custom_map)
            if isinstance(parsed, dict):
                return {str(key): str(value) for key, value in parsed.items()}
        except json.JSONDecodeError:
            pass
    return dict(default_map)


def public_profiles(config: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> Dict[str, Any]:
    prepared = prepare_profile(config, env)
    public: Dict[str, Any] = {
        "active": prepared["active"],
        "profiles_file": prepared["profiles_file"],
        "loaded": prepared["loaded"],
        "profiles": {},
    }
    profiles = config.get("profiles") or {}
    if not isinstance(profiles, dict):
        return public

    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        profile_public = sanitize(profile)
        providers = profile.get("providers") or {}
        profile_public["providers"] = {}
        if isinstance(providers, dict):
            for provider_name in providers.keys():
                active_name, instance = provider_instance(profile, provider_name, env)
                profile_public["providers"][provider_name] = {
                    "active": active_name,
                    "settings": sanitize(instance),
                }
        public["profiles"][name] = profile_public
    return public


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.lower() for marker in SECRET_MARKERS):
                if key_text.endswith("_env"):
                    clean[key_text] = item
                else:
                    clean[key_text] = "***"
            else:
                clean[key_text] = sanitize(item)
        return clean
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value
