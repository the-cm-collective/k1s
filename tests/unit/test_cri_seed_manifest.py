from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CRI_SEED_MANIFEST = ROOT / "lab" / "variants" / "cri_seed_images.lock.json"


def test_cri_seed_manifest_preloads_inference_runtime_images() -> None:
    payload = json.loads(CRI_SEED_MANIFEST.read_text(encoding="utf-8"))
    core_images = payload["images"]["core"]

    assert "docker.io/rayproject/ray:latest" in core_images
    assert "docker.io/vllm/vllm-openai:latest" in core_images


def test_cri_seed_manifest_exposes_lean_bootstrap_profile() -> None:
    payload = json.loads(CRI_SEED_MANIFEST.read_text(encoding="utf-8"))
    bootstrap_images = payload["images"]["bootstrap"]

    assert "docker.io/rayproject/ray:latest" not in bootstrap_images
    assert "docker.io/vllm/vllm-openai:latest" not in bootstrap_images
    assert "nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1" in bootstrap_images
