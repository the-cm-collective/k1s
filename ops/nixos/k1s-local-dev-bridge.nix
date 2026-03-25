{ lib, ... }:

let
  bridgeRoot = "/var/lib/k1s-dev";
  extraHostsFile = /. + "${bridgeRoot}/extra-hosts";
  certDir = /. + "${bridgeRoot}/certs";
  certEntries =
    if builtins.pathExists certDir then
      builtins.readDir certDir
    else
      {};
  managedCertFiles =
    map
      (name: /. + "${bridgeRoot}/certs/${name}")
      (lib.filter
        (name:
          let
            kind = certEntries.${name};
          in
          kind == "regular" && (lib.hasSuffix ".crt" name || lib.hasSuffix ".pem" name))
        (lib.attrNames certEntries));
in
{
  # `make demo` / `make dev-local` populate /var/lib/k1s-dev and then invoke
  # `nixos-rebuild --impure switch` so this module can bridge mutable dev state
  # into NixOS-native /etc/hosts and system CA trust.
  networking.extraHosts = lib.mkIf (builtins.pathExists extraHostsFile) (builtins.readFile extraHostsFile);
  security.pki.certificateFiles = managedCertFiles;
}
