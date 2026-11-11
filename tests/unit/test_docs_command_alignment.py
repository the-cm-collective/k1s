from __future__ import annotations

import argparse
import re
from pathlib import Path

from ae.kctl.__main__ import build_parser as build_k1s_parser

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
START_HERE = ROOT / "docs" / "getting-started" / "start-here.md"
DEMOS = ROOT / "docs" / "guides" / "demos-examples.md"
RUNTIME_PROFILES = ROOT / "docs" / "guides" / "runtime-profiles.md"
ROLLOUTS = ROOT / "docs" / "reference" / "rollouts.md"
K1S_CLI = ROOT / "docs" / "reference" / "k1s-cli.md"
INFERENCE_FABRIC = ROOT / "docs" / "reference" / "inference-fabric.md"
MULTINODE_LAB = ROOT / "docs" / "guides" / "multinode-lab.md"
PYTHON_DISTRIBUTION = ROOT / "docs" / "reference" / "python-distribution.md"
README = ROOT / "README.md"

FENCED_BLOCK_RE = re.compile(r"^```[^\n]*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
INSTALLED_PROFILE_COMMANDS = [
    "python -m pip install k1s-core",
    "k1s --help",
    "k1s profile list",
    "k1s profile doctor core",
    "k1s profile plan core",
    "k1s profile doctor edge-gateway",
    "k1s profile plan edge-gateway",
    "k1s profile doctor node",
    "k1s profile plan node",
]
SOURCE_ONLY_MARKERS = (
    "make ",
    "python -m ae.",
    "scripts/",
    "specs/",
    "state/",
    "ops/",
    ".venv",
)
REPOSITORY_BLOCK_MARKERS = (*SOURCE_ONLY_MARKERS, "docs/")


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


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
    assert "exec $(MAKE) _strict-cri-smoke-root;" in text
    assert (
        "AE_STRICT_CRI_PROFILE_SMOKE=1 AE_CRI_IT=1 AE_CRI_SMOKE_PULL=1 "
        "./scripts/dev/strict_cri_smoke.sh" in text
    )
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


def test_make_demo_embeds_labs_token_for_seeded_local_demo() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "DOCS_EMBED_LABS_TOKEN=$${DOCS_EMBED_LABS_TOKEN:-1}" in text


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


def test_k1s_cli_reference_covers_installed_command_tree() -> None:
    text = K1S_CLI.read_text(encoding="utf-8")
    commands = _subcommands(build_k1s_parser())
    assert set(commands) == {
        "apply",
        "delete",
        "describe",
        "events",
        "get",
        "logs",
        "profile",
        "rollout",
        "scale",
    }
    for command in commands:
        assert f"`k1s {command}" in text

    assert set(_subcommands(commands["rollout"])) == {"history", "undo"}

    profile = _subcommands(commands["profile"])
    assert set(profile) == {
        "bootstrap",
        "delete",
        "doctor",
        "host",
        "list",
        "logs",
        "migrate",
        "plan",
        "restart",
        "run",
        "start",
        "status",
        "stop",
    }
    for command in profile:
        assert f"k1s profile {command}" in text

    bootstrap = _subcommands(profile["bootstrap"])
    assert set(bootstrap) == {"create"}
    assert set(_subcommands(bootstrap["create"])) == {"edge-gateway", "node"}

    assert set(_subcommands(profile["migrate"])) == {"apply", "plan", "rollback"}
    host = _subcommands(profile["host"])
    assert set(host) == {"plan", "prepare", "rollback"}
    for command in host:
        assert set(_subcommands(host[command])) == {"strict-cri"}


def test_multinode_lab_installed_section_uses_only_shipped_package_commands() -> None:
    text = MULTINODE_LAB.read_text(encoding="utf-8")
    section = _section(
        text,
        "## Installed package contract",
        "## Contributor/development live lab",
    )
    blocks = FENCED_BLOCK_RE.findall(section)
    assert len(blocks) == 1
    assert blocks[0].strip().splitlines() == INSTALLED_PROFILE_COMMANDS
    lowered = section.lower()
    for marker in SOURCE_ONLY_MARKERS:
        assert marker not in lowered
    assert "source-validated installed contract" in section
    assert "live WorkerBee" in section
    assert "CRI/containerd" in section


def test_multinode_lab_fenced_block_classification_is_exhaustive() -> None:
    text = MULTINODE_LAB.read_text(encoding="utf-8")
    blocks = FENCED_BLOCK_RE.findall(text)
    assert len(blocks) == 112

    package_cli = {0}
    illustrative_output = {21}
    advanced_ae_cli = {36, 42, 60, 61, 110}
    host_prerequisite = {12, 24, 28, 31, 35, 37, 43, 44, 56, 66, 67, 73, 91, 97, 105}
    classified = package_cli | illustrative_output | advanced_ae_cli | host_prerequisite
    repository_development = set(range(len(blocks))) - classified

    assert len(package_cli) == 1
    assert len(advanced_ae_cli) == 5
    assert len(repository_development) == 90
    assert len(host_prerequisite) == 15
    assert len(illustrative_output) == 1
    assert (
        package_cli
        | advanced_ae_cli
        | repository_development
        | host_prerequisite
        | illustrative_output
    ) == set(range(len(blocks)))

    assert blocks[0].strip().splitlines() == INSTALLED_PROFILE_COMMANDS
    assert "INFO" in blocks[next(iter(illustrative_output))]
    for index in advanced_ae_cli:
        assert re.search(r"(?m)^ae (?:events|nodes|shell|status)\b", blocks[index])
    for index in repository_development:
        assert any(marker in blocks[index].lower() for marker in REPOSITORY_BLOCK_MARKERS)


def test_public_install_sections_reject_source_checkout_commands() -> None:
    readme = README.read_text(encoding="utf-8")
    readme_section = _section(
        readme,
        "## Quickstart: installed package contract",
        "### Contributor and source-checkout development",
    )
    lab = MULTINODE_LAB.read_text(encoding="utf-8")
    lab_section = _section(
        lab,
        "## Installed package contract",
        "## Contributor/development live lab",
    )
    cli = K1S_CLI.read_text(encoding="utf-8")
    cli_section = _section(cli, "## Install and inspect", "## Application commands")
    distribution = PYTHON_DISTRIBUTION.read_text(encoding="utf-8")
    distribution_section = _section(
        distribution,
        "## Install and inspect",
        "## Preserved compatibility surfaces",
    )

    for section in (readme_section, lab_section, cli_section, distribution_section):
        for command in INSTALLED_PROFILE_COMMANDS:
            assert command in section
        blocks = FENCED_BLOCK_RE.findall(section)
        assert blocks
        for block in blocks:
            lowered = block.lower()
            for marker in SOURCE_ONLY_MARKERS:
                assert marker not in lowered

    assert readme.index("python -m pip install k1s-core") < readme.index(
        "python -m pip install -e .[dev]"
    )
    assert "source-checkout development material" in " ".join(cli_section.split())
    assert "source-checkout development material" in " ".join(distribution_section.split())
    contributor = lab.split("## Contributor/development live lab", 1)[1]
    assert "make k1s-core-cri" in contributor
    assert "python -m ae.controller" in contributor
    assert "scripts/dev/" in contributor


def test_public_inference_reference_uses_application_neutral_examples() -> None:
    text = INFERENCE_FABRIC.read_text(encoding="utf-8").lower()
    for project_specific_term in (
        "nigerian",
        "ng-translation",
        "hausa",
        "igbo",
        "yoruba",
        "nitda",
    ):
        assert project_specific_term not in text
