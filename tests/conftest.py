"""Script-style suites (top-level asserts / sys.exit) cannot be collected by pytest
directly; they are executed as subprocesses by test_script_suites.py instead."""

SCRIPT_SUITES = [
    "test_anthropic_404_fallback.py",
    "test_azure_fallback.py",
    "test_opus_frontier_mapping.py",
    "test_openai_compat.py",
    "test_profiles.py",
    "test_azure_ai_status_and_mapping.py",
    "test_azure_discovery.py",
]

collect_ignore = list(SCRIPT_SUITES)
