{
  description = "GitLab Default Branch Setter - Set default branches for all repos in a group";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        pythonPackages = pkgs.python311Packages;
        
        gitlab-branch-setter = pkgs.stdenv.mkDerivation rec {
          pname = "gitlab-branch-setter";
          version = "1.0.0";
          
          src = ./.;
          
          buildInputs = with pythonPackages; [
            python
            python-gitlab
            packaging
            tqdm
          ];
          
          nativeBuildInputs = [ pkgs.makeWrapper ];
          
          installPhase = ''
            mkdir -p $out/bin
            cp set_default_branches.py $out/bin/gitlab-branch-setter
            chmod +x $out/bin/gitlab-branch-setter
            
            # Wrap the script with required Python packages
            wrapProgram $out/bin/gitlab-branch-setter \
              --prefix PYTHONPATH : ${pythonPackages.makePythonPath buildInputs}
          '';
          
          meta = with pkgs.lib; {
            description = "Set default branches for all GitLab repositories in a group";
            license = licenses.mit;
            platforms = platforms.all;
          };
        };
        
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            python311
            pythonPackages.python-gitlab
            pythonPackages.packaging
            pythonPackages.tqdm
            pythonPackages.black
            pythonPackages.flake8
            pythonPackages.mypy
            pythonPackages.pytest
          ];
          
          shellHook = ''
            echo "GitLab Branch Setter Development Environment"
            echo "============================================"
            echo ""
            echo "Python version: $(python --version)"
            echo ""
            echo "Available commands:"
            echo "  python set_default_branches.py  - Run the script"
            echo "  black .                         - Format code"
            echo "  flake8 .                        - Lint code"
            echo "  mypy set_default_branches.py    - Type check"
            echo ""
            echo "Make sure to have your GitLab token at ~/.creds/xos_gitlab_token"
          '';
        };
        
      in
      {
        packages = {
          default = gitlab-branch-setter;
          gitlab-branch-setter = gitlab-branch-setter;
        };
        
        apps.default = flake-utils.lib.mkApp {
          drv = gitlab-branch-setter;
          name = "gitlab-branch-setter";
        };
        
        devShells.default = devShell;
      });
}
