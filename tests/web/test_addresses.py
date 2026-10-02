import ipaddress

import pytest

from swarmeval.web import is_public


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",  # CGNAT
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",  # multicast, which Python counts as global
        "192.0.2.1",  # documentation
        "::1",
        "fe80::1",
        "fd00::1",
        "ff0e::1",  # global-scope multicast
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:169.254.169.254",
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1, which Python counts as global
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address", ["8.8.8.8", "93.184.215.14", "2606:4700::1111", "64:ff9b::808:808"]
)
def test_public_addresses_pass(address: str) -> None:
    assert is_public(ipaddress.ip_address(address))
