from pathlib import Path

from ae.node.rosenpass import (
    KeyMaterial,
    PeerConfig,
    RosenpassConfig,
    RosenpassNodeConfig,
    WireGuardConfig,
    load_config,
    render_rosenpass_stub,
    render_wireguard_config,
)


def test_render_wireguard_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / "rosenpass.yaml"
    cfg_path.write_text(
        """
interface: wg0
wireguard:
  address: 10.42.1.1/24
  listen_port: 51820
peers:
  - name: core
    wg_pubkey: wgpubkey
    allowed_ips:
      - 10.42.0.0/24
    endpoint: 203.0.113.10:51820
    persistent_keepalive: 25
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(cfg_path, base_dir=tmp_path)
    text = render_wireguard_config(config, "privkey")
    assert "[Interface]" in text
    assert "PrivateKey = privkey" in text
    assert "Address = 10.42.1.1/24" in text
    assert "ListenPort = 51820" in text
    assert "[Peer]" in text
    assert "PublicKey = wgpubkey" in text
    assert "AllowedIPs = 10.42.0.0/24" in text
    assert "Endpoint = 203.0.113.10:51820" in text
    assert "PersistentKeepalive = 25" in text


def test_render_rosenpass_stub(tmp_path: Path) -> None:
    wg_cfg = WireGuardConfig(
        interface="wg0",
        address=None,
        listen_port=None,
        mtu=None,
        private_key_path=tmp_path / "wg.key",
        public_key_path=tmp_path / "wg.pub",
    )
    rp_cfg = RosenpassConfig(
        private_key_path=tmp_path / "rosenpass.key",
        public_key_path=tmp_path / "rosenpass.pub",
        listen="0.0.0.0:9999",
        command=None,
        log_level="Quiet",
    )
    config = RosenpassNodeConfig(
        interface="wg0",
        wireguard=wg_cfg,
        rosenpass=rp_cfg,
        peers=[
            PeerConfig(
                name="hub-1",
                wg_pubkey="wgpubkey",
                rosenpass_pubkey="rppubkey",
                allowed_ips=["10.42.0.0/24"],
                endpoint="203.0.113.10:9999",
                persistent_keepalive=25,
                role="initiator",
            )
        ],
        peers_source=None,
    )
    keys = KeyMaterial(
        private_key="cHJpdi1rZXk=",
        public_key="cHViLWtleQ==",
        private_path=tmp_path / "rosenpass.key",
        public_path=tmp_path / "rosenpass.pub",
    )
    stub = render_rosenpass_stub(config, keys)
    assert f'public_key = "{tmp_path / "rosenpass.pub"}"' in stub
    assert f'secret_key = "{tmp_path / "rosenpass.key"}"' in stub
    assert 'listen = ["0.0.0.0:9999"]' in stub
    assert 'public_key = "rppubkey"' in stub
    assert 'endpoint = "203.0.113.10:9999"' in stub
    assert 'exchange_command = ["wg", "set", "wg0", "peer", "wgpubkey", "preshared-key", "/dev/stdin"]' in stub
