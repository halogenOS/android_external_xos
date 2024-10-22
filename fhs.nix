{ pkgs ? import <nixpkgs> {} }:

pkgs.buildFHSUserEnv {
  name = "aosp-env";
  targetPkgs = pkgs: with pkgs; [
      bc
      ccache
      fontconfig
      freetype
      git git-lfs
      git-repo
      glibc.dev
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

      #
      # Optional packages
      #
      crosvm unzip
      # for emulator
      xorg.libxkbfile xorg.libX11 libpulseaudio libpng nss nspr expat libdrm xorg.libxcb
      xorg.libXi xorg.libXext libbsd

      # misc packages
      payload-dumper-go
  ];
  multiPkgs = pkgs: with pkgs; [
  ];
  runScript = "zsh";
  profile = ''
    export LD_LIBRARY_PATH=/usr/lib:/usr/lib32
  '';
}
