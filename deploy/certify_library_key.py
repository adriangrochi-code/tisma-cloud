#!/usr/bin/env python3
"""Certifies the library key of a Tisma Cloud master with the project's key (p2p_library.md 3 quinquies).

The project's private key never leaves the owner's USB stick (app_updates.md, rule 14): this runs on the owner's
computer, like tools/sign_release.py of tisma_engine, with the same key file (PEM, PKCS#8) and openssl.

    1. On the master: tisma-cloud admin library-key        (prints the library key, base64)
    2. Here:          python deploy/certify_library_key.py --key E:/tisma/clave.pem --library-key <base64>
                      [--public-key <64 hex digits>]       (checks it is the project's key: the right USB stick)
    3. On the master: tisma-cloud admin library-cert <the certificate printed by step 2>

The certificate is the Ed25519 signature of the bytes "tisma-library-key/1\\n" followed by the 32 bytes of the library
key. It is checked here with the key's public half before it is printed. Python's standard library and openssl 3 only
(Git Bash on Windows has it).
"""

from __future__ import annotations

import argparse
import base64
import pathlib
import subprocess
import sys
import tempfile

CERT_PREFIX = b"tisma-library-key/1\n"
# The DER of an Ed25519 public key before its 32 bytes (RFC 8410).
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


class Failure(Exception):
    pass


def run(args: list[str]) -> bytes:
    try:
        return subprocess.run(args, check=True, capture_output=True).stdout
    except FileNotFoundError:
        raise Failure(f"{args[0]} no está instalado o no está en el PATH") from None
    except subprocess.CalledProcessError as e:
        raise Failure(f"{' '.join(args[:3])}: {(e.stderr or b'').decode(errors='replace').strip() or 'falló'}") from None


def public_key_raw(openssl: str, key: str) -> bytes:
    der = run([openssl, "pkey", "-in", key, "-pubout", "-outform", "DER"])
    if len(der) != 44 or not der.startswith(ED25519_SPKI_PREFIX):
        raise Failure(f"{key}: no es una clave Ed25519")
    return der[-32:]


def certify(key: str, library_key_b64: str, openssl: str = "openssl", public_hex: str | None = None) -> str:
    """The certificate (base64) of the library key, checked with the project key's public half."""
    try:
        library_key = base64.b64decode(library_key_b64.strip(), validate=True)
    except ValueError:
        raise Failure("la clave de la biblioteca no está en base64") from None
    if len(library_key) != 32:
        raise Failure("la clave de la biblioteca tiene que tener 32 bytes")
    raw = public_key_raw(openssl, key)
    if public_hex is not None and bytes.fromhex(public_hex) != raw:
        raise Failure("la clave no es la de la clave pública dada: ¿es el pendrive correcto?")
    with tempfile.TemporaryDirectory() as work:
        message = pathlib.Path(work) / "library-key.bin"
        message.write_bytes(CERT_PREFIX + library_key)
        signature = run([openssl, "pkeyutl", "-sign", "-rawin", "-inkey", key, "-in", str(message)])
        if len(signature) != 64:
            raise Failure("la firma no tiene 64 bytes")
        pem = pathlib.Path(work) / "public.pem"
        pem.write_text("-----BEGIN PUBLIC KEY-----\n" + base64.encodebytes(ED25519_SPKI_PREFIX + raw).decode()
                       + "-----END PUBLIC KEY-----\n")
        sig_file = pathlib.Path(work) / "cert.sig"
        sig_file.write_bytes(signature)
        run([openssl, "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(pem), "-in", str(message),
             "-sigfile", str(sig_file)])
    return base64.b64encode(signature).decode()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="the project's private key (PEM, PKCS#8), on the USB stick")
    ap.add_argument("--library-key", required=True, help="the master's library key, base64 (admin library-key)")
    ap.add_argument("--public-key", help="the project's public key (64 hex digits), to check the key against it")
    ap.add_argument("--openssl", default="openssl")
    a = ap.parse_args(argv)
    try:
        cert = certify(a.key, a.library_key, a.openssl, a.public_key)
    except Failure as e:
        print(f"No se pudo certificar: {e}", file=sys.stderr)
        return 1
    print("Certificado de la clave de la biblioteca (en el maestro: tisma-cloud admin library-cert <esto>):")
    print(cert)
    return 0


if __name__ == "__main__":
    sys.exit(main())
