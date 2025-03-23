{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";
  };

  outputs = { nixpkgs, ... }:
  let
    forEachSystem = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
    lib = nixpkgs.lib;
  in {
    packages = forEachSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in {
        direnvShell = (pkgs.mkShell {
          packages = [
            (pkgs.writeShellApplication {
              name = "aosp-env";
              text = ''nix develop path:external/xos/devshell'';
            })
          ];
        });
      });
    devShell = forEachSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in (pkgs.buildFHSEnvBubblewrap {
        name = "aosp-env";
        targetPkgs = pkgs: with pkgs; [
          bc
          ccache
          clangStdenv
          fontconfig
          freetype
          gcc
          git git-lfs
          git-repo
          glibc glibc.dev
          gnumake
          imagemagick
          libbsd.dev
          libgcc
          libxcrypt-legacy
          ncurses5
          openssl openssl.dev
          perl
          pkgconf
          pngcrush
          python3
          roboto
          rsync
          unzip
          util-linux
          xmlstarlet
          zip
          zlib
          zsh

          # misc packages
          payload-dumper-go
          strace
        ];
        runScript = "zsh";
        profile = builtins.readFile ((pkgs.formats.keyValue {}).generate "" (
          lib.mapAttrs' (name: value: { name = "export ${name}"; inherit value; }) {
            LIBGCC_DIR = "${pkgs.libgcc.out}/lib/gcc/${pkgs.libgcc.stdenv.buildPlatform.config}/${pkgs.libgcc.version}";
            FONTCONFIG_FILE = with pkgs; makeFontsConf { fontDirectories = [ roboto ]; };
            LD_LIBRARY_PATH="/usr/lib:/usr/lib32";
            DIRENV_DISABLE = 1;
          }
        ));
      }).env
    );
  };
}