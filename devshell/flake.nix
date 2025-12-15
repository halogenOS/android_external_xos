{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-25.11";
  };

  outputs =
    { nixpkgs, ... }:
    let
      forEachSystem = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
      lib = nixpkgs.lib;
      fhs =
        name: pkgs: attrs: additionalPkgs:
        pkgs.buildFHSEnvBubblewrap (
          {
            inherit name;
            targetPkgs =
              pkgs:
              with pkgs;
              [
                bc
                bison
                ccache
                clang_20
                clangStdenv
                dtc
                elfutils
                elfutils.dev
                flex
                fontconfig
                freetype
                gcc
                gcc.cc
                git
                git-lfs
                git-repo
                glibc
                glibc.dev
                pkgsi686Linux.glibc.dev
                gnumake
                go
                imagemagick
                libbsd.dev
                libelf
                libgcc
                lld_20
                llvm_20
                libxcrypt-legacy
                ncurses5
                ncurses
                ncurses.dev
                openssl
                openssl.dev
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

                # some SDK tools need this
                alsa-lib

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
                crosvm
                dbus
                mesa
              ]
              ++ (with pkgs.xorg; [
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
              ]) ++ additionalPkgs;
            profile =
              let
                bootanimPythonEnv = pkgs.python3.withPackages (ps: with ps; [
                  pillow
                  numpy
                ]);
              in
              builtins.readFile (
              (pkgs.formats.keyValue { }).generate "" (
                lib.mapAttrs'
                  (name: value: {
                    name = "export ${name}";
                    inherit value;
                  })
                  {
                    LIBGCC_DIR = "${pkgs.libgcc.out}/lib/gcc/${pkgs.libgcc.stdenv.buildPlatform.config}/${pkgs.libgcc.version}";
                    FONTCONFIG_FILE = with pkgs; makeFontsConf { fontDirectories = [ roboto ]; };
                    BOOTANIM_PYTHON_ENV="${bootanimPythonEnv}";
                  }
              )
            );
            extraBuildCommands =
              let
                cuttlefishCapabilities = pkgs.writeShellScript "" ''
                  #!${pkgs.runtimeShell}
                  echo "capability_check"
                  echo "qemu_cli"
                  echo "crosvm"
                  echo -n "vsock"
                '';
              in
              ''
                mkdir -p $out/usr/lib64/cuttlefish-common/bin
                ln -s ${cuttlefishCapabilities} $out/usr/lib64/cuttlefish-common/bin/capability_query.py
              '';
          }
          // attrs
        );
    in
    rec {
      packages = forEachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          direnvShell = (
            pkgs.mkShell {
              packages = [
                (pkgs.writeShellApplication {
                  name = "aosp-env";
                  text = ''nix develop path:external/xos/devshell'';
                })
                packages.${system}.execShell
              ];
            }
          );
          execShell = fhs "exec-aosp-env" pkgs { } [ ];
          aautoShell =
            let
              pkgs = import nixpkgs { inherit system; };
            in
            (fhs "aauto-env" pkgs { runScript = "zsh"; } [ pkgs.llvmPackages_20.libcxx ]).env;
        }
      );
      devShell = forEachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        (fhs "aosp-env" pkgs { runScript = "zsh"; } [ ]).env
      );
      formatter = forEachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        pkgs.nixfmt-rfc-style
      );
    };
}

