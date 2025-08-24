{
  description = "XOS Tools - Android development utilities";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        lib = pkgs.lib;

        python3Packages = pkgs.python311Packages;

        # XOS common module as a Python package
        xos-common = python3Packages.buildPythonPackage rec {
          pname = "xos-common";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            gitpython
            lxml
            pygithub
          ];

          installPhase = ''
            mkdir -p $out/${python3Packages.python.sitePackages}
            cp xos_common.py $out/${python3Packages.python.sitePackages}/
          '';
        };

        # Mirror all script as a Python application
        mirror-all = python3Packages.buildPythonApplication rec {
          pname = "mirror-all";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            gitpython
            tqdm
            lxml
            rich
            pygithub
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp mirror_all.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Create snapshot script as a Python application
        create-snapshot = python3Packages.buildPythonApplication rec {
          pname = "create-snapshot";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            gitpython
            lxml
            rich
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp create_snapshot.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Merge upstream script as a Python application
        merge-upstream = python3Packages.buildPythonApplication rec {
          pname = "merge-upstream";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            gitpython
            lxml
            rich
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp merge_upstream.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Development shell
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            git
            repo
            xmlstarlet
          ] ++ (with python3Packages; [
            python
            gitpython
            tqdm
            lxml
            rich
            pygithub
            black
            flake8
            mypy
            pytest
            ipython
          ]);

          shellHook = ''
            echo "XOS Tools Development Environment"
            echo "================================="
            echo ""
            echo "Python version: $(python --version)"
            echo ""
            echo "Available scripts:"
            echo "  python mirror_all.py        - Mirror repositories"
            echo "  python create_snapshot.py   - Create snapshot tags"
            echo "  python merge_upstream.py    - Merge upstream changes"
            echo ""
            echo "Make sure TOP is set and build/envsetup.sh is sourced"
          '';
        };

      in
      {
        packages = {
          default = create-snapshot;
          mirror-all = mirror-all;
          create-snapshot = create-snapshot;
          merge-upstream = merge-upstream;
          xos-common = xos-common;
        };

        apps = {
          default = {
            type = "app";
            program = lib.getExe create-snapshot;
          };
          mirror-all = {
            type = "app";
            program = lib.getExe mirror-all;
          };
          create-snapshot = {
            type = "app";
            program = lib.getExe create-snapshot;
          };
          merge-upstream = {
            type = "app";
            program = lib.getExe merge-upstream;
          };
        };

        devShells.default = devShell;
      });
}
