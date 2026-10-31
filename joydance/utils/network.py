from ipaddress import IPv4Address, IPv4Network


def is_private_ipv4(address: str) -> bool:
    """Recognize RFC1918 LAN addresses, excluding other non-global ranges."""
    try:
        ip = IPv4Address(address)
    except ValueError:
        return False
    return any(ip in IPv4Network(network) for network in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    ))
