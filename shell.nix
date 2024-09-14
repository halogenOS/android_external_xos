{ pkgs ? import <nixpkgs> {} }:

let fhs = pkgs.buildFHSUserEnv {
  name = "aosp-env";
  targetPkgs = pkgs: with pkgs; [
      bc
      ccache
      fontconfig
      freetype
      git
      git-repo
      glibc.dev
      gnumake
      imagemagick
      libbsd.dev
      libgcc
      libxcrypt-legacy
      ncurses5
      openssl
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
  ];
  multiPkgs = pkgs: with pkgs; [
  ];
  runScript = "zsh";
  profile = ''
    export ALLOW_NINJA_ENV=true
    export LD_LIBRARY_PATH=/usr/lib:/usr/lib32
  '';
};
in pkgs.stdenv.mkDerivation {
  name = "aosp-env-shell";
  nativeBuildInputs = [ fhs ];
  shellHook = "exec aosp-env";
  FONTCONFIG_FILE = with pkgs; makeFontsConf { fontDirectories = [ roboto ]; };
}
