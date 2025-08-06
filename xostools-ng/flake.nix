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
          version = "16.0";
          
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
          version = "16.0";
          
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
            echo "  python mirror_all.py    - Mirror repositories"
            echo ""
            echo "Make sure TOP is set and build/envsetup.sh is sourced"
          '';
        };
        
      in
      {
        packages = {
          default = mirror-all;
          mirror-all = mirror-all;
          xos-common = xos-common;
        };
        
        apps = {
          default = {
            type = "app";
            program = lib.getExe mirror-all;
          };
          mirror-all = {
            type = "app";
            program = lib.getExe mirror-all;
          };
        };
        
        devShells.default = devShell;
      });
}
