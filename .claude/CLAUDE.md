
## CCEM APM Integration

- **APM Dashboard**: http://localhost:3031
- **APM Config**: /Users/jeremiah/Developer/claude-code-proxy/apm/apm_config.json
- **APM Port**: 3031
- **Skills Path**: ~/.claude/skills/
- **APM Log**: ~/Developer/ccem/apm/hooks/apm_server.log

## Formation Deploy

`/formation deploy` — always run as a background task. Return status messaging only upon completion.

## Attribution Policy

Never include "Generated with Claude Code", "Co-Authored-By: Claude", or any AI/Claude attribution in:
- Pull request bodies or titles
- Commit messages
- Issue comments
- Any externally submitted content (GitHub, GitLab, etc.)

This is a hard rule with no exceptions.

## LiteLLM Security & Supply Chain Policy

**CRITICAL CONSTRAINT**: LiteLLM versions are subject to active supply chain attacks.

### Banned Versions (NEVER install)
- **1.82.7, 1.82.8** — Malicious credential-stealing code (TeamPCP attack, March 2026)
- Any version 1.82.x (blocked by constraint `<1.82.0`)

### Version Constraint
- **Pinned**: `litellm>=1.77.7,<1.82.0` in `pyproject.toml`
- **Rationale**: Blocks malicious 1.82.x while maintaining bug fixes in 1.77.x range

### CVEs Mitigated
- CVE-2024-6825 (RCE) — Affects 1.40.3-1.40.12 (PROTECTED by >=1.77.7)
- CVE-2024-8984 (DoS) — Affects <1.56.2 (PROTECTED by >=1.77.7)

### Detection Checklist
If LiteLLM 1.82.7 or 1.82.8 ever installs:
1. IMMEDIATELY rotate: AWS keys, GCP service accounts, Azure credentials, SSH keys, CI/CD tokens
2. Audit: git history for suspicious changes
3. Check: CloudTrail/Azure Logs for unauthorized API calls
4. Run: `pip audit` for other vulnerable packages
5. Document: incident timeline for security review

### References
- [Sonatype: Compromised litellm PyPI Package](https://www.sonatype.com/blog/compromised-litellm-pypi-package-delivers-multi-stage-credential-stealer)
- [Snyk: Poisoned Security Scanner Backdooring LiteLLM](https://snyk.io/articles/poisoned-security-scanner-backdooring-litellm/)
- [Full Policy](./LITELLM_VULNERABILITY_POLICY.md)
