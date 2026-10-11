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
            requests
            rich
            rapidfuzz
            tqdm
            packaging
            pkgs.git-lfs
          ];

          installPhase = ''
            mkdir -p $out/${python3Packages.python.sitePackages}
            cp xos_common.py $out/${python3Packages.python.sitePackages}/
            cp git_credential_prompts.py $out/${python3Packages.python.sitePackages}/
          '';
        };

        # Mirror all script as a Python application
        mirror-all = python3Packages.buildPythonApplication rec {
          pname = "mirror-all";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp mirror_all.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Mirror drift report as a Python application
        mirror-drift = python3Packages.buildPythonApplication rec {
          pname = "mirror-drift";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            mkdir -p $out/${python3Packages.python.sitePackages}
            cp clean_mirror.py $out/${python3Packages.python.sitePackages}/
            cp mirror_progress.py $out/${python3Packages.python.sitePackages}/
            cp mirror_drift.py $out/bin/${pname}
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

          propagatedBuildInputs = [
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

          propagatedBuildInputs = [
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

          propagatedBuildInputs = [
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
            requests
            beautifulsoup4
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

        # Forgejo branch setter script as a Python application
        forgejo-branch-setter = python3Packages.buildPythonApplication rec {
          pname = "forgejo-branch-setter";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp set_default_branches.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # GitHub branch setter script as a Python application
        github-branch-setter = python3Packages.buildPythonApplication rec {
          pname = "github-branch-setter";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp set_default_branches_github.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };


        # Forgejo webhook manager script as a Python application
        forgejo-webhook-manager = python3Packages.buildPythonApplication rec {
          pname = "forgejo-webhook-manager";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp manage_webhooks.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # List changes script as a Python application
        list-changes = python3Packages.buildPythonApplication rec {
          pname = "list-changes";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp list_changes.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Branch merger script as a Python application
        merge-branches = python3Packages.buildPythonApplication rec {
          pname = "merge-branches";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = [
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            cp merge_branches.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Verify bulletin script as a Python application
        verify-bulletin = python3Packages.buildPythonApplication rec {
          pname = "verify-bulletin";
          version = "1.0";

          src = ./.;

          format = "other";

          propagatedBuildInputs = with python3Packages; [
            requests
            beautifulsoup4
            xos-common
          ];

          installPhase = ''
            mkdir -p $out/bin
            mkdir -p $out/${python3Packages.python.sitePackages}
            cp fetch_bulletin.py $out/${python3Packages.python.sitePackages}/
            cp git_lock.py $out/${python3Packages.python.sitePackages}/
            cp verify_bulletin.py $out/bin/${pname}
            chmod +x $out/bin/${pname}
          '';

          meta.mainProgram = pname;
        };

        # Interpreter carrying every runtime dependency the test suite imports
        pythonTestEnv = python3Packages.python.withPackages (ps: with ps; [
          pytest
          gitpython
          lxml
          pygithub
          requests
          rich
          rapidfuzz
          tqdm
          packaging
          beautifulsoup4
        ]);

        # Offline unit tests: everything is mocked, nothing touches a service
        pytestCheck = pkgs.runCommand "xostools-ng-pytest"
          {
            nativeBuildInputs = [ pythonTestEnv ];
          }
          ''
            cp -r ${./.} source
            chmod -R u+w source
            cd source
            export HOME="$TMPDIR"
            pytest -q tests
            touch $out
          '';

        # Development shell
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            git
            git-lfs
            git-repo
            xmlstarlet
          ] ++ (with python3Packages; [
            python
            gitpython
            tqdm
            lxml
            rich
            pygithub
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
            echo "  python mirror_drift.py      - Report Forgejo and GitHub mirror drift"
            echo "  python create_snapshot.py   - Create snapshot tags"
            echo "  python merge_upstream.py    - Merge upstream changes"
            echo "  python reticulate_splines.py - Reticulate splines (create branches from upstream)"
            echo "  python fetch_bulletin.py    - Fetch Android security bulletins as JSON"
            echo "  python cherry_pick_bulletin.py - Cherry-pick security patches from bulletins"
            echo "  python set_default_branches.py - Set default branches for Forgejo repositories"
            echo "  python set_default_branches_github.py - Set default branches for GitHub repositories"
            echo "  python manage_webhooks.py   - Manage Forgejo webhooks for repositories"
            echo "  python merge_branches.py    - Merge branches across all repositories"
            echo "  python verify_bulletin.py   - Verify security patches from bulletins"
            echo ""
            echo "Make sure TOP is set and build/envsetup.sh is sourced"
          '';
        };

      in
      {
        packages = {
          default = create-snapshot;
          mirror-all = mirror-all;
          mirror-drift = mirror-drift;
          create-snapshot = create-snapshot;
          merge-upstream = merge-upstream;
          reticulate-splines = reticulate-splines;
          fetch-bulletin = fetch-bulletin;
          cherry-pick-bulletin = cherry-pick-bulletin;
          xos-common = xos-common;
          forgejo-branch-setter = forgejo-branch-setter;
          github-branch-setter = github-branch-setter;
          forgejo-webhook-manager = forgejo-webhook-manager;
          list-changes = list-changes;
          merge-branches = merge-branches;
          verify-bulletin = verify-bulletin;
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
          mirror-drift = {
            type = "app";
            program = lib.getExe mirror-drift;
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
          forgejo-branch-setter = {
            type = "app";
            program = lib.getExe forgejo-branch-setter;
          };
          github-branch-setter = {
            type = "app";
            program = lib.getExe github-branch-setter;
          };
          forgejo-webhook-manager = {
            type = "app";
            program = lib.getExe forgejo-webhook-manager;
          };
          list-changes = {
            type = "app";
            program = lib.getExe list-changes;
          };
          merge-branches = {
            type = "app";
            program = lib.getExe merge-branches;
          };
          verify-bulletin = {
            type = "app";
            program = lib.getExe verify-bulletin;
          };
        };

        checks = {
          pytest = pytestCheck;
        };

        devShells.default = devShell;
      });
}
