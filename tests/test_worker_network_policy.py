"""Prove the dispatcher admits exactly the importer's public IPv4 address set."""
import ipaddress
from pathlib import Path
import re

from server.media_sources import SourceImportError, _public_ipv4


POLICY = Path(__file__).resolve().parents[1] / 'web/lib/worker-network-policy.ts'
ORIGINAL_PRIVATE_RANGES = tuple(map(ipaddress.IPv4Network, (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
    '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/4', '240.0.0.0/4',
)))


def public_networks():
    declaration = POLICY.read_text().split('Object.freeze([', 1)[1].split(']);', 1)[0]
    values = re.findall(r"'([^']+)'", declaration)
    networks = tuple(ipaddress.IPv4Network(value, strict=True) for value in values)
    assert len(networks) == 105
    assert all(network.prefixlen > 0 for network in networks)
    assert len(networks) == len(set(networks))
    assert list(networks) == list(ipaddress.collapse_addresses(networks))
    return networks


def accepted_by_worker(address):
    try:
        _public_ipv4(str(address))
        return True
    except SourceImportError:
        return False


def test_public_cidr_policy_preserves_every_original_private_ip_exclusion():
    networks = public_networks()
    assert all(not public.overlaps(private)
               for public in networks for private in ORIGINAL_PRIVATE_RANGES)


def test_every_ipv4_address_matches_the_real_worker_guard_without_public_omissions():
    networks = public_networks()
    # Membership is constant between these CIDR boundaries. Checking each
    # complete interval proves all 2^32 addresses, rather than sampling IPs.
    classifier_ranges = list(ipaddress._IPv4Constants._private_networks)
    classifier_ranges += list(ipaddress._IPv4Constants._private_networks_exceptions)
    classifier_ranges += [ipaddress.IPv4Network('100.64.0.0/10'),
                          ipaddress.IPv4Network('224.0.0.0/4'),
                          ipaddress.IPv4Network('240.0.0.0/4')]
    boundaries = {0, 2**32}
    for network in (*networks, *classifier_ranges, *ORIGINAL_PRIVATE_RANGES):
        boundaries.add(int(network.network_address))
        boundaries.add(int(network.broadcast_address) + 1)
    boundaries = sorted(boundaries)
    covered = allowed = 0
    for low, high in zip(boundaries, boundaries[1:]):
        expected = accepted_by_worker(ipaddress.IPv4Address(low))
        assert accepted_by_worker(ipaddress.IPv4Address(high - 1)) == expected
        for value in (low, high - 1):
            actual = any(ipaddress.IPv4Address(value) in network for network in networks)
            assert actual == expected, f'Public IPv4 policy mismatch at {ipaddress.IPv4Address(value)}'
        covered += high - low
        allowed += high - low if expected else 0
    assert covered == 2**32
    assert allowed == sum(network.num_addresses for network in networks)
    assert allowed == 3_702_258_690


def test_legitimate_public_special_addresses_are_not_lost_to_a_broader_deny_list():
    networks = public_networks()
    for value in ('192.0.0.9', '192.0.0.10', '192.88.99.1', '192.88.99.255'):
        address = ipaddress.IPv4Address(value)
        assert accepted_by_worker(address)
        assert any(address in network for network in networks)
    for value in ('192.0.0.8', '192.0.0.11', '192.0.0.170', '198.18.0.1', '203.0.113.1'):
        address = ipaddress.IPv4Address(value)
        assert not accepted_by_worker(address)
        assert not any(address in network for network in networks)
