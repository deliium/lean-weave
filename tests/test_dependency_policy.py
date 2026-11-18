import re
import socket
import tomllib
from pathlib import Path

import pytest


def test_runtime_dependency_policy():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )
    names = {
        re.split(r"[<>=!~;\[]", dep)[0].lower().replace("_", "-")
        for dep in project["project"]["dependencies"]
    }
    # Narrow runtime exception: the DeepSeek gateway uses the OpenAI-compatible
    # SDK. No other agent, MCP, Docker, or orchestration framework is allowed.
    assert "openai" in names
    forbidden = {
        name for name in names
        if name in {"langgraph", "mcp", "docker"}
        or name.startswith(("mcp-", "docker-", "langgraph-"))
    }
    assert not forbidden, f"Forbidden runtime dependencies: {sorted(forbidden)}"
    assert project["dependency-groups"]["spikes"]
    assert project["project"]["optional-dependencies"]["spikes"]


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_socket_guard(family, method):
    with socket.socket(family) as sock:
        with pytest.raises(RuntimeError, match="network marker"):
            getattr(sock, method)(("127.0.0.1" if family == socket.AF_INET else "::1", 9))


def test_create_connection_guard():
    with pytest.raises(RuntimeError, match="network marker"):
        socket.create_connection(("localhost", 9))


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_unix_socket_compatible(tmp_path, method):
    address = str(tmp_path / "socket")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(address)
        server.listen()
        with socket.socket(socket.AF_UNIX) as client:
            result = getattr(client, method)(address)
            assert result in (None, 0)
            connection, _ = server.accept()
            connection.close()
