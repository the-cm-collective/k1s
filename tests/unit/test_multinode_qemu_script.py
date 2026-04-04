from pathlib import Path


SCRIPT = Path("ops/ci/multinode-qemu.sh")
GITIGNORE = Path(".gitignore")


def test_multinode_qemu_script_uses_ephemeral_default_keys() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'KEY_DIR="$STATE_DIR/keys"' in text
    assert 'generate_keypair "$SSH_KEY_PATH"' in text
    assert 'HOST_KEY_PATH="${HOST_KEY_PATH:-}"' in text
    assert 'HOST_KEY_PATH="${HOST_KEY_PATH:-ops/ci/keys/id_rsa}"' not in text
    assert 'GUEST_HOST_KEY_PATH="${GUEST_HOST_KEY_PATH:-/home/ae/.ssh/ci_host_key}"' in text
    assert "install_guest_host_key" in text


def test_gitignore_blocks_ci_key_directory() -> None:
    text = GITIGNORE.read_text(encoding="utf-8")

    assert "ops/ci/keys/" in text
