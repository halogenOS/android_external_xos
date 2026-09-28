# SPDX-License-Identifier: Apache-2.0
{
  buildGoModule,
  fetchgit,
}:

buildGoModule {
  pname = "siso";
  version = "1.5.8";

  src = fetchgit {
    url = "https://chromium.googlesource.com/build";
    rev = "342ffcda73fb0acb5666792b95c00efc32b47318";
    sparseCheckout = [ "siso" ];
    hash = "sha256-ZZlARi8sFjHtyKtuyPZmOTkp7TQAZVEESY/O1uNomx8=";
  };

  modRoot = "siso";
  vendorHash = "sha256-L7T2CMR3kLZFVyNdCpW1aLzKdIe8gw/CmmEtq4UC7SQ=";

  patches = [ ./siso-abs-builddir.patch ];

  # soong_ui runs a siso found on PATH only when the first line of its version
  # answer ends with this suffix.
  postPatch = ''
    substituteInPlace siso/main.go --replace-fail \
      'const versionStr = "siso " + versionID' \
      'const versionStr = "siso " + versionID + "+absolute-outputs"'
  '';

  env.CGO_ENABLED = 0;

  doCheck = false;

  meta.mainProgram = "siso";
}
