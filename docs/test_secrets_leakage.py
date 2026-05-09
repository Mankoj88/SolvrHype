"""
Security tests — Tier 5.

Test IDs: SEC1–SEC8 from master guide.
Focus: prevent leakage of secrets (private keys, API tokens) via logs,
Telegram messages, or tracebacks.

Run: pytest tests/security/ -v -m security
"""

import logging
import re
import subprocess
import sys

import pytest

pytestmark = pytest.mark.security


# Sensitive patterns that must NEVER appear in logs/Telegram/output.
SENSITIVE_PATTERNS = [
    r"0x[a-fA-F0-9]{64}",                 # Ethereum private key
    r"sk-ant-[a-zA-Z0-9-]{20,}",          # Anthropic API key
    r"[0-9]{8,10}:[A-Za-z0-9_-]{35}",     # Telegram bot token
]


# =============================================================================
# SEC1 — No secrets in log output
# =============================================================================
class TestSecretsNotInLogs:
    def test_no_private_key_in_log_messages(self, monkeypatch, caplog):
        monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "deadbeef" * 8)
        with caplog.at_level(logging.DEBUG):
            # Trigger a flow that uses the private key
            # from execution.order_manager import OrderManager
            # om = OrderManager(...)
            # om.startup()
            pass

        all_log_text = "\n".join(r.getMessage() for r in caplog.records)
        for pattern in SENSITIVE_PATTERNS:
            matches = re.findall(pattern, all_log_text)
            assert not matches, f"Sensitive pattern leaked: {matches[0][:20]}..."
        pytest.skip("Wire after OrderManager exists")


# =============================================================================
# SEC2 — No secrets in Telegram messages
# =============================================================================
class TestSecretsNotInTelegram:
    @pytest.mark.asyncio
    async def test_telegram_messages_scrubbed(self, mock_telegram, monkeypatch):
        # Trigger an error path that might include the private key in error msg
        # await send_alert(f"Error connecting: {os.environ['HL_PRIVATE_KEY'][:20]}")
        for call in mock_telegram.calls:
            for pattern in SENSITIVE_PATTERNS:
                assert not re.search(pattern, call["text"]), \
                    f"Secret leaked in Telegram: {pattern}"
        pytest.skip("Wire telegram module")


# =============================================================================
# SEC3 — No secrets in tracebacks reported externally
# =============================================================================
class TestSecretsNotInTracebacks:
    def test_exception_does_not_include_env_dump(self, monkeypatch):
        """If bot crashes and traceback is sent to Telegram, env vars must
        be scrubbed (no PRIVATE_KEY in locals)."""
        monkeypatch.setenv("HL_PRIVATE_KEY", "0x" + "ab" * 32)
        try:
            raise RuntimeError("Test error")
        except RuntimeError:
            import traceback
            tb_text = traceback.format_exc()
            # In a real test, the bot's exception handler should scrub this
            # before sending to Telegram
            pytest.skip("Wire global exception handler with scrubbing")


# =============================================================================
# SEC4 — Static security scan with bandit
# =============================================================================
class TestBanditScan:
    @pytest.mark.skip_ci
    def test_bandit_no_high_severity(self):
        """Run bandit; fail on any HIGH severity finding."""
        result = subprocess.run(
            ["bandit", "-r", ".", "-x", "tests/,venv/,backtests/", "-f", "json", "-ll"],
            capture_output=True, text=True
        )
        # bandit returns 1 if issues found
        if result.returncode != 0:
            import json
            try:
                report = json.loads(result.stdout)
                high = [r for r in report.get("results", [])
                        if r["issue_severity"] == "HIGH"]
                if high:
                    pytest.fail(f"Bandit HIGH severity issues:\n" +
                                "\n".join(f"  {r['filename']}:{r['line_number']} "
                                          f"{r['test_id']} {r['issue_text']}"
                                          for r in high))
            except json.JSONDecodeError:
                pytest.fail(f"Bandit failed:\n{result.stdout}\n{result.stderr}")


# =============================================================================
# SEC5 — File permissions check (.env, state files)
# =============================================================================
class TestFilePermissions:
    @pytest.mark.skip_ci
    def test_env_file_is_600(self):
        """In production, .env should be chmod 600."""
        import os
        from pathlib import Path
        env = Path(".env")
        if not env.exists():
            pytest.skip(".env not present in test env")
        mode = env.stat().st_mode & 0o777
        assert mode == 0o600, f".env has mode {oct(mode)}, expected 0o600"


# =============================================================================
# SEC6 — Dependencies vulnerability scan
# =============================================================================
class TestDependenciesVuln:
    @pytest.mark.skip_ci
    def test_no_known_critical_vulns(self):
        """Run pip-audit. Skip if not installed."""
        try:
            result = subprocess.run(
                ["pip-audit", "--format=json"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                pytest.fail(f"pip-audit found vulns:\n{result.stdout[:2000]}")
        except FileNotFoundError:
            pytest.skip("pip-audit not installed")


# =============================================================================
# SEC7 — Hardcoded secrets scan
# =============================================================================
class TestNoHardcodedSecrets:
    def test_no_obvious_secrets_in_code(self):
        import os
        from pathlib import Path

        bad_patterns = [
            (re.compile(r'private_key\s*=\s*["\']0x[a-f0-9]{64}["\']'), "private key"),
            (re.compile(r'api_key\s*=\s*["\']sk-ant-[a-zA-Z0-9-]+["\']'),
             "anthropic key"),
            (re.compile(r'bot_token\s*=\s*["\'][0-9]{8,}:[A-Za-z0-9_-]{35}["\']'),
             "telegram token"),
        ]

        findings = []
        # Scan project files (excluding tests, venv)
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if d not in {"tests", "venv", ".git",
                                                     "node_modules", "__pycache__"}]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = Path(root) / f
                try:
                    text = path.read_text()
                except (UnicodeDecodeError, PermissionError):
                    continue
                for pattern, label in bad_patterns:
                    for m in pattern.finditer(text):
                        findings.append((str(path), label,
                                         text[:m.start()].count("\n") + 1))

        assert not findings, f"Hardcoded secrets found: {findings}"
