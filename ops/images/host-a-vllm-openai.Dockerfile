ARG BASE_IMAGE=nvidia/cuda:12.1.0-devel-ubuntu22.04
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CUDA_HOME=/usr/local/cuda \
    PATH=/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ARG TORCH_VERSION=2.4.0
ARG TORCHVISION_VERSION=0.19.0
ARG TORCHAUDIO_VERSION=2.4.0
ARG VLLM_VERSION=0.6.2
ARG TRANSFORMERS_VERSION=4.45.0
ENV PIP_PREFER_BINARY=1

WORKDIR /workspace

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libnuma1 \
    python-is-python3 \
    python3 \
    python3-pip \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip setuptools wheel

RUN python3 -m pip install \
    --index-url https://download.pytorch.org/whl/cu121 \
    torch==${TORCH_VERSION} \
    torchvision==${TORCHVISION_VERSION} \
    torchaudio==${TORCHAUDIO_VERSION}

# Keep transformers pinned for the torch 2.4 / CUDA 12.1 lane. Newer 5.x
# releases currently break the api_server import smoke during image build.
RUN python3 -m pip install --prefer-binary \
    vllm==${VLLM_VERSION} \
    transformers==${TRANSFORMERS_VERSION}

COPY ops/images/pyairports_shim/ /tmp/pyairports_shim/

# outlines imports pyairports at module import time, but the current
# pyairports wheel on PyPI only ships dist-info and sample files.
RUN python3 - <<'PY'
import pathlib
import shutil
import site

source = pathlib.Path("/tmp/pyairports_shim/pyairports")
site_dir = pathlib.Path(next(path for path in site.getsitepackages() if path.endswith("dist-packages")))
shutil.copytree(source, site_dir / "pyairports", dirs_exist_ok=True)
PY

RUN python3 - <<'PY'
import vllm.entrypoints.openai.api_server
print("vllm import smoke ok")
PY

EXPOSE 8000

ENTRYPOINT ["python3", "-m", "vllm.entrypoints.openai.api_server"]
