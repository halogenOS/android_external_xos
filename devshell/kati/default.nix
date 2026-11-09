# SPDX-FileCopyrightText: The halogenOS Project
# SPDX-License-Identifier: Apache-2.0
{
  stdenv,
  mimalloc,
  src,
}:

stdenv.mkDerivation {
  pname = "kati";
  version = src.shortRev;

  inherit src;

  # The glibc allocator uses a large share of the CPU time used by Kati.
  # Linked in, mimalloc replaces malloc and free for the whole process.
  buildInputs = [ mimalloc ];
  env.NIX_LDFLAGS = "-lmimalloc";

  # -march=native would tie the binary to the CPU of the machine that built it,
  # and the compiler wrapper drops it anyway, with a warning for every file.
  # The source has no .git directory here, so the revision that Kati shows in
  # its version answer and in the Ninja files it writes comes from the input.
  makeFlags = [
    "-f"
    "Makefile.ckati"
    "KATI_ARCH_FLAGS="
    "KATI_VERSION=${src.rev}"
  ];
  buildFlags = [ "ckati" ];
  enableParallelBuilding = true;

  installPhase = ''
    runHook preInstall
    install -Dm755 ckati $out/bin/ckati
    runHook postInstall
  '';

  meta.mainProgram = "ckati";
}
