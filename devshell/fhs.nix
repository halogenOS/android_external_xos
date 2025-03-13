{ pkgs ? import <nixpkgs> {} }:

pkgs.buildFHSUserEnv {
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
  ];
  multiPkgs = pkgs: [];
  runScript = "zsh";
  profile = ''
    export LD_LIBRARY_PATH=/usr/lib:/usr/lib32
    export LIBGCC_DIR="$(dirname $(${pkgs.gcc.out}/bin/gcc -print-libgcc-file-name))"
  '';
}
