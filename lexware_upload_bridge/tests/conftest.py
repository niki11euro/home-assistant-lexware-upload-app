"""Explicitly report loopback tests blocked by an execution sandbox."""
import socket
import pytest


@pytest.fixture(scope='session')
def loopback_available():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(('127.0.0.1', 0))
        except PermissionError:
            return False
    return True


@pytest.fixture(autouse=True)
def require_loopback_when_marked(request):
    if request.node.get_closest_marker('loopback') and not request.getfixturevalue('loopback_available'):
        pytest.skip('Execution environment forbids binding temporary localhost test servers')
