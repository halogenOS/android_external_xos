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
  vendorHash = "sha256-XFOO5HL9/IuYuA20ul4ZdJESgGIE2ZclgENch6x+Mnk=";

  env.CGO_ENABLED = 0;

  doCheck = false;

  meta.mainProgram = "siso";
}
