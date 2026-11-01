from ipaddress import IPv4Address, IPv4Network
import re
from urllib.parse import urlparse


def is_private_ipv4(address: str) -> bool:
    """Recognize RFC1918 LAN addresses, excluding other non-global ranges."""
    try:
        ip = IPv4Address(address)
    except ValueError:
        return False
    return any(ip in IPv4Network(network) for network in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    ))


def is_local_ipv4(address):
    try:
        return is_private_ipv4(address) or IPv4Address(address).is_loopback
    except (ValueError, TypeError):
        return False


def normalize_console_url(value):
    """Accept only local IPv4 endpoints advertised by legacy games or relays."""
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value):
        raise ValueError("Invalid discovered console address.")
    try:
        uri = urlparse(value)
        if (uri.scheme not in {"http", "https", "ws", "wss"} or not is_local_ipv4(uri.hostname)
                or uri.username or uri.password or uri.fragment or uri.port == 0):
            raise ValueError
    except ValueError as error:
        raise ValueError("Invalid discovered console address.") from error
    path = uri.path.rstrip("/")
    if not path.endswith("/smartphone"):
        path += "/smartphone"
    return uri._replace(scheme="ws", path=path).geturl()
