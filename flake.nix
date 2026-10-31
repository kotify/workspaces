{
  description = "wt: git worktrees with their own ports and process-compose stacks";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAll = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      python = pkgs: pkgs.python3.withPackages (p: [ p.pytest ]);
      devTools = pkgs: [
        (python pkgs)
        pkgs.direnv
        pkgs.git
        pkgs.ruff
        pkgs.pyright
      ];
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.stdenvNoCC.mkDerivation {
          pname = "wt";
          version = lib.fileContents ./VERSION;
          src = lib.fileset.toSource {
            root = ./.;
            fileset = lib.fileset.unions [
              ./wt.py
              ./VERSION
              ./LICENSE
              ./skills
            ];
          };
          nativeBuildInputs = [ pkgs.makeWrapper ];
          # Appended, so a repo's own tools (for example its process-compose) win.
          installPhase = ''
            mkdir -p $out/bin $out/share/wt $out/share/doc/wt
            cp -r wt.py VERSION skills $out/share/wt/
            cp LICENSE $out/share/doc/wt/
            makeWrapper ${pkgs.python3}/bin/python3 $out/bin/wt \
              --add-flags $out/share/wt/wt.py \
              --suffix PATH : ${
                lib.makeBinPath (
                  [
                    pkgs.git
                    pkgs.direnv
                    pkgs.process-compose
                    pkgs.lsof
                  ]
                  ++ lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.procps ]
                )
              }
          '';
          meta = {
            description = "Git worktrees with their own ports and process-compose stacks";
            homepage = "https://github.com/kotify/workspaces";
            license = lib.licenses.mit;
            mainProgram = "wt";
            platforms = lib.platforms.unix;
          };
        };
      });

      checks = forAll (pkgs: {
        package = self.packages.${pkgs.system}.default;
        tests = pkgs.runCommand "wt-tests" { nativeBuildInputs = devTools pkgs; } ''
          cp -r ${self} src
          chmod -R +w src
          cd src
          export HOME=$TMPDIR
          ruff format --check .
          ruff check .
          pyright
          pytest -q
          touch $out
        '';
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell { packages = devTools pkgs; };
      });
    };
}
