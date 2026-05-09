# Solvira — Stress Test Suite

Panduan lengkap stress test untuk Solvira AI Trading Bot.

## 📚 Master Guide

Baca dulu: [`solvira_stress_test_master.md`](./solvira_stress_test_master.md) — dokumen utama berisi 6 tier test, 22 regression test untuk known bugs, decision gate, dan jadwal eksekusi 4 minggu.

## 🚀 Quick Start

```bash
# 1. Salin folder ini ke root proyek Solvira
cp -r solvira_stress_test/tests <project>/
cp solvira_stress_test/pytest.ini <project>/
cp solvira_stress_test/requirements-dev.txt <project>/

# 2. Install dependencies test
cd <project>
pip install -r requirements-dev.txt

# 3. Wire-up: setiap file test punya `pytest.skip("Wire ...")` placeholder.
#    Hapus skip dan import modul Solvira yang sebenarnya. Contoh di
#    tests/regression/test_known_bugs.py — uncomment baris `from execution...`.

# 4. Jalankan test bertahap:
pytest tests/unit/         -v -m unit          # Tier 1 — paling cepat
pytest tests/integration/  -v -m integration   # Tier 2
pytest tests/chaos/        -v -m chaos         # Tier 3
pytest tests/regression/   -v -m regression    # Tier 6 — KRITIS
pytest tests/security/     -v -m security      # Tier 5
pytest tests/endurance/    -v -m endurance     # Tier 4 — slow

# 5. Coverage report
pytest --cov=. --cov-report=html
open htmlcov/index.html
```

## 📁 Struktur Folder

```
solvira_stress_test/
├── solvira_stress_test_master.md    ← MAIN GUIDE (baca ini dulu)
├── pytest.ini                       ← config pytest + markers
├── requirements-dev.txt             ← dev dependencies
└── tests/
    ├── conftest.py                  ← fixtures (mock HL, DB, telegram, dll)
    ├── unit/                        ← Tier 1 — module-level tests
    │   ├── test_indicators.py
    │   └── test_allocation_manager.py
    ├── integration/                 ← Tier 2 — multi-module pipeline
    ├── chaos/                       ← Tier 3 — fault injection
    │   └── test_fault_injection.py
    ├── endurance/                   ← Tier 4 — long-running stability
    │   └── test_long_running.py
    ├── regression/                  ← Tier 6 — 22 known bugs ⚠️ MUST PASS
    │   └── test_known_bugs.py
    └── security/                    ← Tier 5 — secret leakage, bandit
        └── test_secrets_leakage.py
```

## 🎯 Severity Markers

| Marker | Arti | Behavior |
|--------|------|----------|
| `@pytest.mark.blocker` | 🔴 Blocking — wajib pass sebelum mainnet | Fail = stop deployment |
| `@pytest.mark.major` | 🟡 Major — sebaiknya pass | Document jika di-skip |
| `@pytest.mark.minor` | 🟢 Minor — nice-to-have | Optional |
| `@pytest.mark.slow` | Test >30 detik | Run terpisah |
| `@pytest.mark.requires_testnet` | Butuh HL testnet aktif | Skip di CI |

Filter cepat:
```bash
pytest -m blocker                    # hanya blocker
pytest -m "not slow and not endurance"  # quick suite untuk dev
pytest -m "regression and blocker"   # regression bugs critical only
```

## 🐛 Regression Tests — 22 Known Bugs

File: `tests/regression/test_known_bugs.py`

Setiap class `TestBugXX` test satu bug yang sudah identified di code review. **JANGAN HAPUS** test ini — mereka guard agar bug yang sama tidak balik lagi.

Bug paling kritis (BLOCKER):
- **Bug #1** — Scanner look-ahead bias (iloc[-1] vs iloc[-2])
- **Bug #2** — TP2 sizing math wrong
- **Bug #3** — SL doesn't fully close
- **Bug #5** — No startup reconciliation

## 📅 Jadwal Eksekusi

Lihat **Appendix B** di master guide. Singkatnya:

```
Week 1 → Tier 1-3 (unit, integration, chaos) — bug fix loop
Week 2 → Tier 4-6 (endurance, security, regression) + 7-day testnet DRY
Week 3-4 → 14-day testnet live + decision gate
```

## ⚠️ Reminder Sebelum Mainnet

1. **JANGAN SKIP** Tier 6 regression tests
2. **JANGAN SKIP** 14-hari testnet validation
3. **JANGAN SKIP** small ($5-10) mainnet withdraw test sebelum kapital penuh

Detail di section "End-to-End Manual Testnet Validation" master guide.

---

**Catatan:** Semua file test sudah mengandung `pytest.skip("Wire ...")` placeholders. Setelah Anda uncomment import dari modul Solvira yang sebenarnya, hapus baris `pytest.skip(...)` di akhir setiap test untuk mengaktifkannya.
