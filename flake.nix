{
  description = "proxmox-kea-integration";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";

  outputs = { self, nixpkgs }:
    let
      pkgs = import nixpkgs { system = "x86_64-linux"; };
      pyPkgs = ps: with ps; [ flask docker black ];
    in {
      devShells.x86_64-linux.default = pkgs.mkShell {
        name = "proxmox-kea-integration";

        packages = with pkgs; [ (python3.withPackages pyPkgs) ];
      };
    };
}
