{ pkgs ? import <nixpkgs> {} }:

let fhs = pkgs.buildFHSUserEnv {
  name = "aosp-env";
  targetPkgs = pkgs: with pkgs; [
      bc
      ccache
      git
      git-repo
      gnumake
      imagemagick
      openssl
      perl
      pngcrush
      python3
      unzip
      util-linux
      xmlstarlet
      zip
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
}
