# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Shared real-HTTP fixtures for inference and credential transport tests."""

import socket
import ssl
import threading

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@contextmanager
def serving_rebinding(handler, payload):
    """Run approved and private stand-ins that DNS can select on the same port."""

    class Approved(handler):
        pass

    class Private(handler):
        pass

    Approved.payload = payload
    Approved.seen = []
    Private.payload = payload
    Private.seen = []
    approved_server = ThreadingHTTPServer(("127.0.0.1", 0), Approved)
    port = approved_server.server_address[1]
    private_server = ThreadingHTTPServer(("127.0.0.2", port), Private)
    servers = (approved_server, private_server)
    threads = tuple(threading.Thread(target=server.serve_forever, daemon=True) for server in servers)
    for thread in threads:
        thread.start()
    try:
        yield port, Approved.seen, Private.seen
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def rebinding_dns(original):
    """Return the approved address to a trust lookup and a private one to a normal lookup."""

    def getaddrinfo(host, port, *args, **kwargs):
        proto = kwargs.get("proto", args[2] if len(args) > 2 else 0)
        if host == "localhost":
            address = "127.0.0.1" if proto == socket.IPPROTO_TCP else "127.0.0.2"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
        return original(host, port, *args, **kwargs)

    return getaddrinfo


def local_dns(original):
    """Resolve localhost to the TLS stand-in and leave numeric addresses unchanged."""

    def getaddrinfo(host, port, *args, **kwargs):
        if host == "localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
        return original(host, port, *args, **kwargs)

    return getaddrinfo


def issue_server_certificate(directory, hostname):
    """Write an ephemeral CA and one server certificate for hostname."""
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Inference transport test CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    certificate_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, certificate_path, key_path


@contextmanager
def serving_tls(handler, payload, certificate_path, key_path):
    """Run a TLS stand-in and record the SNI hostname."""

    class Handler(handler):
        pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, _request, _client_address):
            """The wrong-certificate case resets the socket before an HTTP request exists."""

    Handler.payload = payload
    Handler.seen = []
    server_names = []
    server = Server(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    context.set_servername_callback(lambda _socket, name, _context: server_names.append(name))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], Handler.seen, server_names
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


__all__ = ("issue_server_certificate", "local_dns", "rebinding_dns", "serving_rebinding", "serving_tls")
