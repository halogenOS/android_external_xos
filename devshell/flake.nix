{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";
  };

  outputs = { nixpkgs, ... }:
  let
    forEachSystem = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
    lib = nixpkgs.lib;
    fhs = name: pkgs: attrs: pkgs.buildFHSEnvBubblewrap ({
        inherit name;
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

          # for emulator
          libpulseaudio
          libpng
          nss
          nspr
          expat
          libdrm
          libbsd
          xcb-util-cursor
          libxkbcommon
          libsForQt5.qt5.qtwayland
          xwayland
        ] ++ (with pkgs.xorg; [
          libX11
          libXcursor
          libxcb
          xcbutilimage
          xcbutilwm
          xcbutilkeysyms
          xcbutilrenderutil
          libXi
          libXext
          libxkbfile
          libSM
          libICE
        ]);
        profile = builtins.readFile ((pkgs.formats.keyValue {}).generate "" (
          lib.mapAttrs' (name: value: { name = "export ${name}"; inherit value; }) {
            LIBGCC_DIR = "${pkgs.libgcc.out}/lib/gcc/${pkgs.libgcc.stdenv.buildPlatform.config}/${pkgs.libgcc.version}";
            FONTCONFIG_FILE = with pkgs; makeFontsConf { fontDirectories = [ roboto ]; };
            LD_LIBRARY_PATH="/usr/lib:/usr/lib32";
          }
        ));
      } // attrs);
  in rec {
    packages = forEachSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in {
        direnvShell = (pkgs.mkShell {
          packages = [
            (pkgs.writeShellApplication {
              name = "aosp-env";
              text = ''nix develop path:external/xos/devshell'';
            })
            packages.${system}.execShell
          ];
        });
        execShell = fhs "exec-aosp-env" pkgs {};
      });
    devShell = forEachSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in (fhs "aosp-env" pkgs { runScript = "zsh"; }).env
    );
  };
}