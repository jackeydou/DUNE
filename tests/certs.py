"""A throwaway CA and service certificates shaped like `swarm-certs` writes them, so tests of
mutual TLS need no Go toolchain. The real tool is checked against these services in the tests
marked `docker`."""

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from swarmeval.mtls import Identity


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


class TestCA:
    """`issue(service)` writes `<dir>/<service>/{ca.crt,tls.crt,tls.key}`."""

    __test__ = False

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        self._cert = (
            x509.CertificateBuilder()
            .subject_name(_name("test CA"))
            .issuer_name(_name("test CA"))
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self._key, hashes.SHA256())
        )

    def issue(self, service: str, *, uris: list[str] | None = None) -> Identity:
        """A certificate for `service`, valid as client and as a server on loopback. `uris`
        replaces the identity it names."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        names: list[x509.GeneralName] = [
            x509.UniformResourceIdentifier(uri)
            for uri in (uris if uris is not None else [f"spiffe://swarmeval/{service}"])
        ]
        names += [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(service))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
                ),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )
        out = self._dir / service
        out.mkdir(parents=True, exist_ok=True)
        identity = Identity(cert=out / "tls.crt", key=out / "tls.key", ca=out / "ca.crt")
        _write_key(identity.key, key)
        identity.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        identity.ca.write_bytes(self._cert.public_bytes(serialization.Encoding.PEM))
        return identity
