# SPDX-License-Identifier: Apache-2.0
{
  buildGoModule,
  src,
}:

buildGoModule {
  pname = "siso";
  version = "1.5.8";

  inherit src;

  modRoot = "siso";
  vendorHash = "sha256-L7T2CMR3kLZFVyNdCpW1aLzKdIe8gw/CmmEtq4UC7SQ=";

  env.CGO_ENABLED = 0;

  doCheck = false;

  meta.mainProgram = "siso";
}
