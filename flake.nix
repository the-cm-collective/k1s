{
  description = "k1s additive development shells";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
  };

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = lib.genAttrs systems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python311;
          libPath = lib.makeLibraryPath [
            pkgs.stdenv.cc.cc
            pkgs.openssl
            pkgs.zlib
          ];
          commonPackages = with pkgs; [
            age
            curl
            docker-compose
            gcc
            git
            gnumake
            jq
            mypy
            nssTools
            openssl
            pkg-config
            podman
            podman-compose
            pre-commit
            python
            ruff
            sqlite
            sops
            zlib
            python311Packages.pytest
          ];
          criPackages = with pkgs; [
            containerd
            crictl
            iptables
            nerdctl
          ];
          shellHook = ''
            export PIP_DISABLE_PIP_VERSION_CHECK=1
            export PODMAN_COMPOSE_PROVIDER="''${PODMAN_COMPOSE_PROVIDER:-podman-compose}"
            export LD_LIBRARY_PATH="${libPath}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

            if [ -d "$PWD/.venv" ] && [ -x "$PWD/.venv/bin/python" ]; then
              export VIRTUAL_ENV="$PWD/.venv"
              export PATH="$VIRTUAL_ENV/bin:$PATH"
              echo "[nix-shell] using .venv"
            else
              echo "[nix-shell] bootstrap: python -m venv .venv && . .venv/bin/activate && python -m pip install -e .[dev]"
            fi
          '';
        in
        {
          default = pkgs.mkShell {
            packages = commonPackages;
            inherit shellHook;
          };

          cri = pkgs.mkShell {
            packages = commonPackages ++ criPackages;
            inherit shellHook;
          };
        }
      );
    };
}
