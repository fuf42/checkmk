#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""This module provides commonly used functions for the handling of encrypted
data within the Checkmk ecosystem."""

from __future__ import annotations

import binascii
import contextlib
import os
import re
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtensionOID, NameOID
from OpenSSL import crypto, SSL

from cmk.utils.crypto.secrets import EncrypterSecret
from cmk.utils.crypto.symmetric import aes_gcm_decrypt, aes_gcm_encrypt, TaggedCiphertext
from cmk.utils.exceptions import MKGeneralException

_PEM_RE = re.compile(
    "-----BEGIN CERTIFICATE-----\r?.+?\r?-----END CERTIFICATE-----\r?\n?", re.DOTALL
)


def raw_certificates_from_file(path: Path) -> list[str]:
    try:
        # Some users use comments in certificate files e.g. to write the content of the
        # certificate outside of encapsulation boundaries. We only want to extract the
        # certificates between the encapsulation boundaries which have to be 7-bit ASCII
        # characters.
        with path.open("r", encoding="ascii", errors="surrogateescape") as f:
            return [
                content
                for match in _PEM_RE.finditer(f.read())
                if (content := match.group(0)).isascii()
            ]
    except FileNotFoundError:
        # Silently ignore e.g. dangling symlinks
        return []


class CertificateDetails(NamedTuple):
    issued_to: str
    issued_by: str
    valid_from: str
    valid_till: str
    signature_algorithm: str
    digest_sha256: str
    serial_number: int
    is_ca: bool
    verify_result: ChainVerifyResult


class ChainVerifyResult(NamedTuple):
    cert_pem: bytes
    error_number: int
    error_depth: int
    error_message: str
    is_valid: bool


# NOTE: Use this function only in conjunction with the permission server_side_requests
def fetch_certificate_details(
    trusted_ca_file: Path, address_family: socket.AddressFamily, address: tuple[str, int]
) -> Iterable[CertificateDetails]:
    """Creates a list of certificate details for the chain certs"""
    verify_chain_results = _fetch_certificate_chain_verify_results(
        trusted_ca_file, address_family, address
    )
    if not verify_chain_results:
        raise MKGeneralException("Failed to fetch the certificate chain")

    def get_name(name_obj):
        return name_obj.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value

    for result in verify_chain_results:
        crypto_cert = x509.load_pem_x509_certificate(result.cert_pem, default_backend())
        yield CertificateDetails(
            issued_to=get_name(crypto_cert.subject),
            issued_by=get_name(crypto_cert.issuer),
            valid_from=str(crypto_cert.not_valid_before),
            valid_till=str(crypto_cert.not_valid_after),
            signature_algorithm=crypto_cert.signature_hash_algorithm.name,
            digest_sha256=binascii.hexlify(crypto_cert.fingerprint(hashes.SHA256())).decode(),
            serial_number=crypto_cert.serial_number,
            is_ca=_is_ca_certificate(crypto_cert),
            verify_result=result,
        )


def _is_ca_certificate(crypto_cert: x509.Certificate) -> bool:
    try:
        key_usage = crypto_cert.extensions.get_extension_for_oid(ExtensionOID.KEY_USAGE)
        use_key_for_signing = key_usage.value.key_cert_sign is True
    except x509.ExtensionNotFound:
        use_key_for_signing = False

    try:
        basic_constraints = crypto_cert.extensions.get_extension_for_oid(
            ExtensionOID.BASIC_CONSTRAINTS
        )
        is_ca = basic_constraints.value.ca is True
    except x509.ExtensionNotFound:
        is_ca = False

    return is_ca and use_key_for_signing


def _fetch_certificate_chain_verify_results(
    trusted_ca_file: Path,
    address_family: socket.AddressFamily,
    address: tuple[str, int],
) -> list[ChainVerifyResult]:
    """Opens a SSL connection and performs a handshake to get the certificate chain"""

    ctx = SSL.Context(SSL.SSLv23_METHOD)
    ctx.load_verify_locations(str(trusted_ca_file))

    with contextlib.closing(
        SSL.Connection(ctx, socket.socket(address_family, socket.SOCK_STREAM))
    ) as sock:
        # pylint does not get the object type of sock right
        sock.connect(address)
        sock.do_handshake()
        certificate_store = sock.get_context().get_cert_store()
        certificate_chain = sock.get_peer_cert_chain()

    # No chain, no need to verify
    if not certificate_chain:
        return []

    return _verify_certificate_chain(certificate_store, certificate_chain)


def _verify_certificate_chain(
    x509_store: crypto.X509Store, certificate_chain: list[crypto.X509]
) -> list[ChainVerifyResult]:
    verify_chain_results = []

    # Used to record all certificates and verification results for later displaying
    for cert in certificate_chain:
        # This is mainly done to get the textual error message without accessing internals of the SSL modules
        error_number, error_depth, error_message = 0, 0, ""
        try:
            crypto.X509StoreContext(x509_store, cert).verify_certificate()
        except crypto.X509StoreContextError as e:
            error_number, error_depth, error_message = e.errors

        verify_chain_results.append(
            ChainVerifyResult(
                cert_pem=crypto.dump_certificate(crypto.FILETYPE_PEM, cert),
                error_number=error_number,
                error_depth=error_depth,
                error_message=error_message,
                is_valid=error_number == 0,
            )
        )

    return verify_chain_results


class Encrypter:
    """Helper to encrypt site secrets

    The secrets are encrypted using the auth.secret which is only known to the local and remotely
    configured sites.
    """

    # TODO: This shares almost all the code with PasswordStore, except for the version bytes that
    # are prepended by the store.

    SALT_LENGTH: int = 16
    NONCE_LENGTH: int = 16

    @staticmethod
    def encrypt(value: str) -> bytes:
        salt = os.urandom(Encrypter.SALT_LENGTH)
        nonce = os.urandom(Encrypter.NONCE_LENGTH)
        key = EncrypterSecret().derive_secret_key(salt)
        encrypted = aes_gcm_encrypt(key, nonce, value)
        return salt + nonce + encrypted.tag + encrypted.ciphertext

    @staticmethod
    def decrypt(raw: bytes) -> str:
        salt, rest = raw[: Encrypter.SALT_LENGTH], raw[Encrypter.SALT_LENGTH :]
        nonce, rest = rest[: Encrypter.NONCE_LENGTH], rest[Encrypter.NONCE_LENGTH :]
        tag, encrypted = rest[: TaggedCiphertext.TAG_LENGTH], rest[TaggedCiphertext.TAG_LENGTH :]
        key = EncrypterSecret().derive_secret_key(salt)
        return aes_gcm_decrypt(key, nonce, TaggedCiphertext(ciphertext=encrypted, tag=tag))
