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
            python-gitlab
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

        # Reticulate splines script as a Python application
        reticulate-splines = python3Packages.buildPythonApplication rec {
          pname = "reticulate-splines";
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
            cp reticulate_splines.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Fetch bulletin script as a Python application
        fetch-bulletin = python3Packages.buildPythonApplication rec {
          pname = "fetch-bulletin";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            requests
            beautifulsoup4
            lxml
            gitpython
            rich
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp fetch_bulletin.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Cherry-pick bulletin script as a Python application
        cherry-pick-bulletin = python3Packages.buildPythonApplication rec {
          pname = "cherry-pick-bulletin";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            gitpython
            lxml
            rich
            requests
            beautifulsoup4
            rapidfuzz
            python-gitlab
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            mkdir -p $out/${python3Packages.python.sitePackages}
            cp fetch_bulletin.py $out/${python3Packages.python.sitePackages}/
            cp git_lock.py $out/${python3Packages.python.sitePackages}/
            cp cherry_pick_bulletin.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Development shell
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            git
            git-repo
            xmlstarlet
          ] ++ (with python3Packages; [
            python
            gitpython
            tqdm
            lxml
            rich
            pygithub
            python-gitlab
            requests
            beautifulsoup4
            rapidfuzz
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
            echo "  python reticulate_splines.py - Reticulate splines (create branches from upstream)"
            echo "  python fetch_bulletin.py    - Fetch Android security bulletins as JSON"
            echo "  python cherry_pick_bulletin.py - Cherry-pick security patches from bulletins"
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
          reticulate-splines = reticulate-splines;
          fetch-bulletin = fetch-bulletin;
          cherry-pick-bulletin = cherry-pick-bulletin;
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
          reticulate-splines = {
            type = "app";
            program = lib.getExe reticulate-splines;
          };
          fetch-bulletin = {
            type = "app";
            program = lib.getExe fetch-bulletin;
          };
          cherry-pick-bulletin = {
            type = "app";
            program = lib.getExe cherry-pick-bulletin;
          };
        };

        devShells.default = devShell;
      });
}
