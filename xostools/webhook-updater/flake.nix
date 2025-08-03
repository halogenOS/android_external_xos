{
  description = "GitLab Webhook Manager - Manage push webhooks for all repos in a group";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        pythonPackages = pkgs.python311Packages;
        
        pythonDeps = with pythonPackages; [
          python
          python-gitlab
          tqdm
        ];
        
        gitlab-webhook-manager = pkgs.stdenv.mkDerivation rec {
          pname = "gitlab-webhook-manager";
          version = "1.0.0";
          
          src = ./.;
          
          buildInputs = pythonDeps;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          
          installPhase = ''
            mkdir -p $out/bin
            cp manage_webhooks.py $out/bin/gitlab-webhook-manager
            chmod +x $out/bin/gitlab-webhook-manager
            
            # Wrap the script with required Python packages
            wrapProgram $out/bin/gitlab-webhook-manager \
              --prefix PYTHONPATH : ${pythonPackages.makePythonPath pythonDeps}
          '';
          
          meta = with pkgs.lib; {
            description = "Manage GitLab webhooks for all repositories in a group";
            license = licenses.mit;
            platforms = platforms.all;
          };
        };
        
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            python311
          ] ++ pythonDeps ++ (with pythonPackages; [
            black
            flake8
            mypy
            pytest
            ipython
            types-requests  # Type stubs for mypy
          ]);
          
          shellHook = ''
            echo "GitLab Webhook Manager Development Environment"
            echo "=============================================="
            echo ""
            echo "Python version: $(python --version)"
            echo ""
            echo "Usage:"
            echo "  python manage_webhooks.py <webhook_url>  - Set webhooks for all repos"
            echo "  python manage_webhooks.py --dry-run <webhook_url>  - Preview changes"
            echo ""
            echo "Example:"
            echo "  python manage_webhooks.py https://example.com/webhook/push"
            echo ""
            echo "Development commands:"
            echo "  black .                    - Format code"
            echo "  flake8 .                   - Lint code"
            echo "  mypy manage_webhooks.py    - Type check"
            echo ""
            echo "Make sure to have your GitLab token at ~/.creds/xos_gitlab_token"
          '';
        };
        
      in
      {
        packages = {
          default = gitlab-webhook-manager;
          gitlab-webhook-manager = gitlab-webhook-manager;
        };
        
        apps.default = {
          type = "app";
          program = "${gitlab-webhook-manager}/bin/gitlab-webhook-manager";
        };
        
        devShells.default = devShell;
      });
}
