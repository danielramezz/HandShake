"""
Network / link-layer helpers: find our LAN address, make the TLS
certificates, and print a QR code so phones can join quickly.

How the devices find each other (Lectures 7-8):
  * The Wi-Fi router's DHCP server gives the laptop and every phone a
    PRIVATE IP address in the same subnet (e.g. 192.168.1.0/24).
  * Devices in one subnet reach each other directly through the access
    point, with no router hop in between. So a phone only needs the
    laptop's IP and our port (8443) to open a TCP connection.
  * We print that address (and a QR code of it) when the server starts.
    The server listens on 0.0.0.0, which means "every interface".
"""

import datetime
import ipaddress
import socket
from pathlib import Path

import qrcode
import qrcode.image.svg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def get_lan_ip() -> str:
    """
    The IPv4 address of the interface we would use to reach the network.
    Trick: "connecting" a UDP socket sends no packet. It only makes the OS
    pick a route, and with it the source address of our main interface.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def all_ipv4_addresses() -> list[str]:
    """Every IPv4 address of this machine (Wi-Fi, Ethernet, hotspot ...)."""
    found = {get_lan_ip(), "127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(found)


def is_private(ip: str) -> bool:
    """True for 10/8, 172.16/12 and 192.168/16 (the private ranges behind NAT)."""
    return ipaddress.ip_address(ip).is_private


# ------------------------------------------------------------- TLS ---
#
# Two certificates, like the real web (a tiny Public Key Infrastructure):
#
#   Handshake Local CA   (ca.pem)    made ONCE. Each phone installs and trusts it.
#        | signs
#        v
#   server certificate   (cert.pem)  lists the laptop's current IPs. Re-issued
#                                    automatically when the IP changes; phones
#                                    keep trusting it because the CA stays the same.
#
# Why HTTPS on a LAN? Browsers only allow the Web Crypto API (used for E2EE)
# on secure origins, and iPhones refuse WebSockets to untrusted certificates.
# TLS also encrypts every phone-to-laptop hop on the shared Wi-Fi.

CA_NAME = "Handshake Local CA"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _write_key(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def ensure_ca(cert_dir: Path):
    """Load our certificate authority, creating it the first time."""
    ca_path, ca_key_path = cert_dir / "ca.pem", cert_dir / "ca-key.pem"
    if ca_path.exists() and ca_key_path.exists():
        cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
        key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
        return cert, key

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)                # a root CA signs itself
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(ca_key_path, key)
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def _cert_is_current(cert_path: Path, ca_cert, ips: list[str]) -> bool:
    """Is the server certificate signed by our CA, unexpired, and listing all our IPs?"""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        covered = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        return (cert.issuer == ca_cert.subject
                and set(ips) <= covered
                and cert.not_valid_after_utc > _now() + datetime.timedelta(days=7))
    except Exception:
        return False


def ensure_certificate(cert_dir: Path, ips: list[str]) -> tuple[Path, Path]:
    """Make sure a CA-signed server certificate for our IP addresses exists."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_cert, ca_key = ensure_ca(cert_dir)
    cert_path, key_path = cert_dir / "cert.pem", cert_dir / "key.pem"
    if key_path.exists() and _cert_is_current(cert_path, ca_cert, ips):
        return cert_path, key_path

    key = ec.generate_private_key(ec.SECP256R1())
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips])
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Handshake server")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        # Apple's rules for trusted TLS certificates: at most 825 days, a SAN list,
        # and the "serverAuth" extended key usage.
        .not_valid_after(_now() + datetime.timedelta(days=397))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(key_path, key)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def ca_certificate_der(cert_dir: Path) -> bytes | None:
    """The CA certificate in DER form: the file phones download and install."""
    ca_path = cert_dir / "ca.pem"
    if not ca_path.exists():
        return None
    cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
    return cert.public_bytes(serialization.Encoding.DER)


# -------------------------------------------------------------- QR ---

def qr_svg(text: str) -> str:
    """QR code as an SVG string (shown on the dashboard)."""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


def print_qr(text: str) -> None:
    """QR code drawn in the terminal."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:  # old Windows consoles
        print("(QR code could not be drawn in this terminal; open /dashboard instead)")
