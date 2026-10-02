"""Which addresses `web_request` may connect to: global unicast only.

The worker can reach Postgres, object storage, model-gateway, and the Control API, so this check
is what keeps an agent away from them (trajectory-first spec decision 3).
"""

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def is_public(address: IPAddress) -> bool:
    """IPv6 forms that carry an IPv4 address (mapped, 6to4, Teredo, NAT64) are judged by that
    address. Multicast is refused although Python counts most of it as global."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            return is_public(embedded)
    return address.is_global and not address.is_multicast


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None
