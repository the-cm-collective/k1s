from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
START_HERE = ROOT / "docs" / "getting-started" / "start-here.md"
DEMOS = ROOT / "docs" / "guides" / "demos-examples.md"
RUNTIME_PROFILES = ROOT / "docs" / "guides" / "runtime-profiles.md"
ROLLOUTS = ROOT / "docs" / "reference" / "rollouts.md"


def test_makefile_exposes_premerge_validation_targets() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "profile-smoke:" in text
    assert "strict-cri-smoke:" in text
    assert "docs-verify:" in text
    assert "premerge-dev:" in text
    assert "[profile-smoke] run this target as your normal user." in text
    assert "AE_PROFILE_SMOKE_ALLOW_ROOT" in text
    assert 'exec sudo -E env "PATH=$$PATH" $(MAKE) _profile-smoke-root' not in text
    assert "AE_PROFILE_SMOKE=1 python -m pytest" in text
    assert "if ! sudo -v >/dev/null; then" in text
    assert 'exec sudo -E env "PATH=$$PATH" $(MAKE) _strict-cri-smoke-root;' in text
    assert 'exec $(MAKE) _strict-cri-smoke-root;' in text
    assert "AE_STRICT_CRI_PROFILE_SMOKE=1 AE_CRI_IT=1 AE_CRI_SMOKE_PULL=1 ./scripts/dev/strict_cri_smoke.sh" in text
    assert "DOCS_API_BASE=$${DOCS_API_BASE:-https://api.home.arpa:8443}" in text


def test_start_here_matches_current_demo_and_labs_entrypoints() -> None:
    text = START_HERE.read_text(encoding="utf-8")
    assert "make demo" in text
    assert (
        "`make demo`: run the current seeded demo profile "
        "(blue/green sample apps + docs/api/dashboard)." in text
    )
    assert (
        "`make demo-legacy`: flag-driven `init_demo.sh` wrapper "
        'for legacy demo modes via `ARGS="..."`.' in text
    )
    assert (
        "`make labs-up` / `make labs-down`: host-controller `dev-etcd` "
        "wrapper (CLI/API only; no docs/Caddy)." in text
    )
    assert (
        "`make labs-aio-up` / `make labs-aio-down`: host-controller `dev-etcd` "
        "wrapper with Caddy/TLS and the dev-local helper defaults." in text
    )
    assert (
        "`make labs-apishim-env`: print apishim tokens from the active `dev-etcd` profile "
        "(`state/profiles/dev-etcd/apishim.env` unless `PROFILE_DIR=` overrides it)." in text
    )
    assert 'make demo ARGS="' not in text


def test_demos_examples_uses_demo_legacy_for_flag_driven_modes() -> None:
    text = DEMOS.read_text(encoding="utf-8")
    assert "`make demo` is the current seeded default" in text
    assert '`make demo-legacy ARGS="--demo-standard -y -d"`' in text
    assert '`make demo-legacy ARGS="--docs-only -y -d"`' in text
    assert 'make demo ARGS="' not in text


def test_runtime_and_rollout_docs_reference_current_profile_targets() -> None:
    runtime_text = RUNTIME_PROFILES.read_text(encoding="utf-8")
    assert "make dev-min" in runtime_text
    assert "make dev-etcd" in runtime_text
    assert "make k1s-core-cri" in runtime_text
    assert "make k1s-edge-core-cri" in runtime_text

    rollout_text = ROLLOUTS.read_text(encoding="utf-8")
    assert 'make demo-legacy ARGS="--demo-rollout -y -d"' in rollout_text
    assert 'make demo ARGS="' not in rollout_text
