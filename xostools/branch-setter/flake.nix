{
  description = "GitLab/GitHub Default Branch Setter - Set default branches for all repos in a group/org";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        pythonPackages = pkgs.python311Packages;
        
        # Common Python dependencies
        pythonDeps = with pythonPackages; [
          python
          packaging
          tqdm
        ];
        
        gitlabDeps = pythonDeps ++ [ pythonPackages.python-gitlab ];
        githubDeps = pythonDeps ++ [ pythonPackages.pygithub ];
        allDeps = pythonDeps ++ [ pythonPackages.python-gitlab pythonPackages.pygithub ];
        
        gitlab-branch-setter = pkgs.stdenv.mkDerivation rec {
          pname = "gitlab-branch-setter";
          version = "1.0.0";
          
          src = ./.;
          
          buildInputs = gitlabDeps;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          
          installPhase = ''
            mkdir -p $out/bin
            cp set_default_branches.py $out/bin/gitlab-branch-setter
            chmod +x $out/bin/gitlab-branch-setter
            
            # Wrap the script with required Python packages
            wrapProgram $out/bin/gitlab-branch-setter \
              --prefix PYTHONPATH : ${pythonPackages.makePythonPath gitlabDeps}
          '';
          
          meta = with pkgs.lib; {
            description = "Set default branches for all GitLab repositories in a group";
            license = licenses.mit;
            platforms = platforms.all;
          };
        };
        
        github-branch-setter = pkgs.stdenv.mkDerivation rec {
          pname = "github-branch-setter";
          version = "1.0.0";
          
          src = ./.;
          
          buildInputs = githubDeps;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          
          installPhase = ''
            mkdir -p $out/bin
            cp set_default_branches_github.py $out/bin/github-branch-setter
            chmod +x $out/bin/github-branch-setter
            
            # Wrap the script with required Python packages
            wrapProgram $out/bin/github-branch-setter \
              --prefix PYTHONPATH : ${pythonPackages.makePythonPath githubDeps}
          '';
          
          meta = with pkgs.lib; {
            description = "Set default branches for all GitHub repositories in an organization";
            license = licenses.mit;
            platforms = platforms.all;
          };
        };
        
        branch-setter-all = pkgs.symlinkJoin {
          name = "branch-setter-all";
          paths = [ gitlab-branch-setter github-branch-setter ];
          meta = with pkgs.lib; {
            description = "Set default branches for GitLab and GitHub repositories";
            license = licenses.mit;
            platforms = platforms.all;
          };
        };
        
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            python311
          ] ++ allDeps ++ (with pythonPackages; [
            black
            flake8
            mypy
            pytest
            ipython
          ]);
          
          shellHook = ''
            echo "GitLab/GitHub Branch Setter Development Environment"
            echo "==================================================="
            echo ""
            echo "Python version: $(python --version)"
            echo ""
            echo "Available commands:"
            echo "  python set_default_branches.py         - Run GitLab script"
            echo "  python set_default_branches_github.py  - Run GitHub script"
            echo "  black .                                - Format code"
            echo "  flake8 .                               - Lint code"
            echo "  mypy *.py                              - Type check"
            echo ""
            echo "Make sure to have your tokens at:"
            echo "  ~/.creds/xos_gitlab_token"
            echo "  ~/.creds/xos_github_token"
          '';
        };
        
      in
      {
        packages = {
          default = branch-setter-all;
          all = branch-setter-all;
          gitlab = gitlab-branch-setter;
          github = github-branch-setter;
        };
        
        apps = {
          default = {
            type = "app";
            program = "${branch-setter-all}/bin/gitlab-branch-setter";
          };
          gitlab = {
            type = "app";
            program = "${gitlab-branch-setter}/bin/gitlab-branch-setter";
          };
          github = {
            type = "app";
            program = "${github-branch-setter}/bin/github-branch-setter";
          };
        };
        
        devShells.default = devShell;
      });
}
