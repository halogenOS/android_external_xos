{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";
  };

  outputs = { nixpkgs, ... }:
  let
  forEachSystem = nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed;
  in {
    devShell = forEachSystem (system:
      let pkgs = import nixpkgs { inherit system; };
      in pkgs.mkShell {
        buildInputs = with pkgs; [
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
        ];
        LIBGCC_DIR = "${pkgs.libgcc.out}/lib/gcc/${pkgs.libgcc.stdenv.buildPlatform.config}/${pkgs.libgcc.version}";
      }
    );
  };
}