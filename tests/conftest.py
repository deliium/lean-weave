"""In-process offline guard; child processes and collection are not sandboxed."""

import logging
import socket

import pytest

log = logging.getLogger("lean_weave.tests")


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    if request.node.get_closest_marker("network"):
        log.info("[Tests.offline] guard=bypassed test=%s", request.node.nodeid)
        return
    log.info("[Tests.offline] guard=active test=%s", request.node.nodeid)
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded(original):
        def call(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                log.debug("[Tests.offline] blocked test=%s", request.node.nodeid)
                raise RuntimeError("Network blocked; explicit network marker required")
            return original(sock, address)
        return call

    def block_connection(*args, **kwargs):
        log.debug("[Tests.offline] blocked test=%s", request.node.nodeid)
        raise RuntimeError("Network blocked; explicit network marker required")

    monkeypatch.setattr(socket.socket, "connect", guarded(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(connect_ex))
    monkeypatch.setattr(socket, "create_connection", block_connection)
