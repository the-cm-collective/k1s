{ pkgs, ... }:

{
  virtualisation.containerd = {
    enable = true;
    settings = {
      version = 2;
      plugins."io.containerd.grpc.v1.cri" = {
        cni = {
          bin_dir = "${pkgs.cni-plugins}/bin";
          conf_dir = "/etc/cni/net.d";
        };
        registry.config_path = "/etc/containerd/certs.d";
      };
    };
  };

  environment.systemPackages = with pkgs; [
    cni-plugins
    cri-tools
    iptables
  ];

  environment.etc."crictl.yaml".text = ''
    runtime-endpoint: unix:///run/containerd/containerd.sock
    image-endpoint: unix:///run/containerd/containerd.sock
    timeout: 10
    debug: false
  '';

  environment.etc."cni/net.d/10-k1s-bridge.conflist".text = ''
    {
      "cniVersion": "0.4.0",
      "name": "cni0",
      "plugins": [
        {
          "type": "bridge",
          "bridge": "cni0",
          "isGateway": true,
          "ipMasq": true,
          "promiscMode": true,
          "ipam": {
            "type": "host-local",
            "ranges": [[{ "subnet": "10.88.0.0/16" }]],
            "routes": [{ "dst": "0.0.0.0/0" }]
          }
        },
        { "type": "portmap", "capabilities": { "portMappings": true } },
        { "type": "firewall" },
        { "type": "tuning" }
      ]
    }
  '';

  environment.etc."cni/net.d/99-loopback.conf".text = ''
    {
      "cniVersion": "0.4.0",
      "name": "lo",
      "type": "loopback"
    }
  '';
}
