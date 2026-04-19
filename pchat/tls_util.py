from __future__ import annotations

import ipaddress
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .constants import CERT_FILE, KEY_FILE
from .utils import get_lan_ip


def ensure_self_signed_cert(certs_dir: Path) -> tuple[Path, Path]:
    cert_path = certs_dir / CERT_FILE
    key_path = certs_dir / KEY_FILE
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError(
            "TLS certificate generation requires the 'cryptography' package. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    certs_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname()
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "P-Chat Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )
    alt_names: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    try:
        alt_names.append(x509.IPAddress(ipaddress.ip_address(get_lan_ip())))
    except ValueError:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def create_server_ssl_context(certs_dir: Path) -> ssl.SSLContext:
    cert_path, key_path = ensure_self_signed_cert(certs_dir)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def create_client_ssl_context() -> ssl.SSLContext:
    # Small LAN version: encrypt transport but do not verify the self-signed host cert.
    # This is the extension point for future certificate pinning or CA verification.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
