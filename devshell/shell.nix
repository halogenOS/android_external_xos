{ pkgs ? import <nixpkgs> {} }:

pkgs.stdenv.mkDerivation {
  name = "aosp-env-shell";
  nativeBuildInputs = [ (import ./fhs.nix {}) ];
  shellHook = "exec aosp-env";
  FONTCONFIG_FILE = with pkgs; makeFontsConf { fontDirectories = [ roboto ]; };
}
