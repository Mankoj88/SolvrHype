"""
Security tests — File permissions & operational security.

Source: solvira_stress_test_master.md §7.2 (manual checklist) and §7.3
(network egress audit). These are mostly manual ops checks; the scaffolds
below codify them as pytest tests so they are visible in the suite.
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.security


# =============================================================================
# .env file should be 600 (owner read/write only)
# =============================================================================
class TestEnvFilePermissions:
    @pytest.mark.skip_ci
    def test_env_file_is_600(self):
        """In production, .env should be chmod 600. Skipped in CI / Windows."""
        if os.name == "nt":
            pytest.skip("POSIX permission semantics; skip on Windows")
        env = Path(".env")
        if not env.exists():
            pytest.skip(".env not present in test env")
        mode = env.stat().st_mode & 0o777
        assert mode == 0o600, f".env has mode {oct(mode)}, expected 0o600"


# =============================================================================
# Data directory permissions (DB + state files should be 600)
# =============================================================================
class TestDataDirPermissions:
    @pytest.mark.skip_ci
    def test_data_files_are_600(self):
        if os.name == "nt":
            pytest.skip("POSIX permission semantics; skip on Windows")
        data_dir = Path("data")
        if not data_dir.exists():
            pytest.skip("data/ not present in test env")
        for child in data_dir.iterdir():
            if not child.is_file():
                continue
            mode = child.stat().st_mode & 0o777
            assert mode == 0o600, f"{child} has mode {oct(mode)}, expected 0o600"


# =============================================================================
# .env is gitignored
# =============================================================================
class TestEnvGitignored:
    def test_env_is_gitignored(self):
        gi = Path(".gitignore")
        if not gi.exists():
            pytest.skip(".gitignore not present in test env")
        text = gi.read_text(encoding="utf-8", errors="ignore")
        assert ".env" in text, ".env must be listed in .gitignore"


# =============================================================================
# systemd hardening present (manual operational check)
# =============================================================================
class TestSystemdHardening:
    @pytest.mark.skip_ci
    def test_systemd_unit_has_protect_directives(self):
        """Operational check — verifies ProtectHome / NoNewPrivileges in unit."""
        pytest.skip("Manual check: sudo systemctl cat solvira | grep -E "
                    "'Protect|NoNewPriv'")


# =============================================================================
# Network egress allow-list (manual operational check)
# =============================================================================
class TestNetworkEgressAllowList:
    @pytest.mark.skip_ci
    def test_only_expected_hosts_in_egress(self):
        """Manual: ss -tnp | grep python | awk '{print $5}' | cut -d: -f1
        should yield only api.hyperliquid*, arb1.arbitrum.io, api.telegram.org,
        api.anthropic.com, api.exchangerate*."""
        pytest.skip("Manual operational check (ss/netstat snapshot)")
