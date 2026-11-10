"""«Certificar con mi pendrive» (Manager.certify_library, `tisma-cloud admin library-certify`): the project key on the
owner's USB stick signs the library key once, read into memory only."""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tisma_cloud import admin, library
from tisma_cloud.manager import Manager

ROOT = Path(__file__).resolve().parents[1]


def project_key(folder: Path, password: bytes | None = None) -> tuple[Path, Ed25519PrivateKey]:
    """A project key as tools/sign_release.py of the engine uses it (PEM, PKCS#8), on a "USB stick" folder."""
    key = Ed25519PrivateKey.generate()
    folder.mkdir(parents=True, exist_ok=True)
    enc = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    path = folder / "clave.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc))
    return path, key


def raw_public(key) -> bytes:  # noqa: ANN001
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@pytest.fixture
def master(tmp_path, monkeypatch):
    for name in ("TISMA_LIBRARY_PROJECT_KEY", "TISMA_STORAGE_DIR"):
        monkeypatch.delenv(name, raising=False)
    m = Manager(tmp_path / "master")
    m.set_role("master")
    return m


def certificate_is_good(m: Manager, public: bytes) -> bool:
    key = library.LibraryKey(m.home / "storage")
    cert = key.certificate()
    return cert is not None and library.verify(public, cert, library.CERT_PREFIX + key.public)


def test_one_click_certifies_and_sets_the_project_key(master, tmp_path):
    path, key = project_key(tmp_path / "usb")
    before = path.read_bytes()
    assert master._library_check().fix == "library_cert"
    assert master.certify_library(path) == "Certificada. Ya podés sacar el pendrive."
    assert certificate_is_good(master, raw_public(key))
    assert master.read_env()["TISMA_LIBRARY_PROJECT_KEY"] == raw_public(key).hex()
    check = master._library_check()
    assert check.ok is True and check.fix is None
    # The key stays on the stick, as it was (it signs the app's updates later).
    assert path.read_bytes() == before
    # The master's own check accepts the certificate (the 3-step path installs the same thing).
    lk = library.LibraryKey(master.home / "storage")
    lk.install_certificate(library.b64(lk.certificate()), raw_public(key).hex())


def test_the_private_key_lands_nowhere_and_its_buffer_is_zeroed(master, tmp_path, monkeypatch, caplog, capsys):
    path, key = project_key(tmp_path / "usb")
    pem = path.read_bytes()
    raw = key.private_bytes_raw()
    body = b"".join(pem.splitlines()[1:-1])
    secrets = [raw, raw.hex().encode(), base64.b64encode(raw), body, pem]
    buffers = []
    real = library.read_secret

    def spy(p):  # noqa: ANN001, ANN202
        buffers.append(real(p))
        return buffers[-1]

    monkeypatch.setattr(library, "read_secret", spy)
    caplog.set_level(logging.DEBUG)
    master.certify_library(path)
    assert buffers and all(b == bytearray(len(b)) and len(b) == len(pem) for b in buffers)
    out = capsys.readouterr()
    said = (caplog.text + out.out + out.err).encode()
    for found in [p for p in master.home.rglob("*") if p.is_file()] + [None]:
        data = found.read_bytes() if found else said
        for secret in secrets:
            assert secret not in data, (found, secret[:8])
    # Nothing else next to the key on the stick (no copy, no temporary file).
    assert [p.name for p in (tmp_path / "usb").iterdir()] == ["clave.pem"]


def test_another_key_than_the_configured_one_is_refused(master, tmp_path):
    good, good_key = project_key(tmp_path / "usb")
    wrong, _ = project_key(tmp_path / "other")
    master.certify_library(good)
    cert = (master.home / "storage" / "library.cert").read_bytes()
    with pytest.raises(library.ProjectKeyMismatch, match="pendrive correcto"):
        master.certify_library(wrong)
    assert (master.home / "storage" / "library.cert").read_bytes() == cert
    assert master.read_env()["TISMA_LIBRARY_PROJECT_KEY"] == raw_public(good_key).hex()
    # Also when the project key was set by hand, in base64, before the first certification.
    master.write_env({"TISMA_LIBRARY_PROJECT_KEY": base64.b64encode(raw_public(good_key)).decode()})
    with pytest.raises(library.ProjectKeyMismatch):
        master.certify_library(wrong)


def test_certifying_again_after_a_rotation(master, tmp_path):
    path, key = project_key(tmp_path / "usb")
    master.certify_library(path)
    # The library key rotated (a new library.key: the old certificate is put aside): certified again.
    (master.home / "storage" / "library.key").unlink()
    assert library.LibraryKey(master.home / "storage").certificate() is None
    master.certify_library(path)
    assert certificate_is_good(master, raw_public(key))
    # The project key rotated on purpose: only when asked.
    new, new_key = project_key(tmp_path / "usb2")
    with pytest.raises(library.ProjectKeyMismatch):
        master.certify_library(new)
    master.certify_library(new, replace=True)
    assert certificate_is_good(master, raw_public(new_key))
    assert master.read_env()["TISMA_LIBRARY_PROJECT_KEY"] == raw_public(new_key).hex()


def test_keys_that_are_not_the_project_key(master, tmp_path):
    usb = tmp_path / "usb"
    path, key = project_key(usb, password=b"secreta")
    with pytest.raises(library.KeyPasswordNeeded):
        master.certify_library(path)
    with pytest.raises(library.LibraryError, match="contraseña"):
        master.certify_library(path, "otra")
    assert master.certify_library(path, "secreta").startswith("Certificada")
    assert certificate_is_good(master, raw_public(key))
    ecdsa = usb / "ec.pem"
    ecdsa.write_bytes(ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(library.LibraryError, match="Ed25519"):
        master.certify_library(ecdsa, replace=True)
    junk = usb / "foto.jpg"
    junk.write_bytes(b"\xff\xd8" * 100)
    with pytest.raises(library.LibraryError, match="PEM"):
        master.certify_library(junk)
    huge = usb / "grande.pem"
    huge.write_bytes(b"x" * (library.MAX_PEM_BYTES + 1))
    with pytest.raises(library.LibraryError, match="no es una clave"):
        master.certify_library(huge)
    with pytest.raises(RuntimeError, match="No se pudo leer"):
        master.certify_library(usb / "no-esta.pem")
    # Without the key file, the diagnosis cannot fix it by itself (the window asks for the file).
    with pytest.raises(RuntimeError, match="library-certify"):
        master.fix("library_cert")


def test_the_command_line(master, tmp_path, monkeypatch, capsys):
    path, key = project_key(tmp_path / "usb")
    wrong, _ = project_key(tmp_path / "other")
    monkeypatch.setenv("TISMA_HOME", str(master.home))
    monkeypatch.chdir(master.home)
    assert admin.main(["library-certify", "--key", str(path)]) == 0
    assert "Certificada" in capsys.readouterr().out
    assert admin.main(["library-certify", "--key", str(wrong)]) == 1
    assert "TISMA_LIBRARY_PROJECT_KEY" in capsys.readouterr().err
    assert certificate_is_good(master, raw_public(key))
    assert admin.main(["library-certify", "--key", str(wrong), "--replace-project-key"]) == 0


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_the_same_certificate_as_the_three_step_path(master, tmp_path):
    """Ed25519 signatures are deterministic: deploy/certify_library_key.py (openssl) gives the same bytes."""
    path, _ = project_key(tmp_path / "usb")
    master.certify_library(path)
    lk = library.LibraryKey(master.home / "storage")
    run = subprocess.run([sys.executable, str(ROOT / "deploy" / "certify_library_key.py"), "--key", str(path),
                          "--library-key", library.b64(lk.public)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert library.b64(lk.certificate()) in run.stdout
