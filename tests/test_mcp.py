"""The read-only MCP connection, served by the real app and read by the SDK's own client."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx2
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from shelfsaver import mcp_server, swap
from shelfsaver.config import Config
from shelfsaver.db import Database, ensure_mcp_token
from shelfsaver.web.app import create_app
from tests.test_web import PASSWORD, FakeScanner, FakeWorker

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def fake_volume(tmp_path, monkeypatch):
    volume = tmp_path / "volume"
    monkeypatch.setattr(swap, "_volume_root", lambda path: str(volume) + "\\")


@pytest.fixture
def parts(tmp_path):
    db = Database(tmp_path / "data" / "shelfsaver.db")
    worker = FakeWorker()
    worker.db = db
    return db, worker, FakeScanner(), Config(data_dir=tmp_path / "data")


@pytest.fixture
def token(parts):
    return ensure_mcp_token(parts[0])


@pytest.fixture
def live(parts):
    """The real app on a free port, shut down and checked for a clean stop afterwards."""
    db, worker, scanner, config = parts
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(db, worker, scanner, config),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive(), "the app did not shut down cleanly"


def session(live, token):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(f"{live}/mcp", http_client=http))


def call(live, token, name, arguments=None):
    async def go():
        async with session(live, token) as client:
            result = await client.call_tool(name, arguments or {})
            assert not result.is_error, result
            data = result.structured_content or json.loads(result.content[0].text)
            return data.get("result", data) if isinstance(data, dict) else data
    return asyncio.run(go())


def test_mcp_requires_token(parts, token):
    db, worker, scanner, config = parts
    with TestClient(create_app(db, worker, scanner, config)) as client:
        for url in ("/mcp", "/mcp/"):
            assert client.post(url, json=INIT, headers=MCP_HEADERS).status_code == 401
            wrong = {**MCP_HEADERS, "Authorization": "Bearer nope"}
            assert client.post(url, json=INIT, headers=wrong).status_code == 401
            other = {**MCP_HEADERS, "Authorization": f"Basic {token}"}
            assert client.post(url, json=INIT, headers=other).status_code == 401
        # A signed-in browser session is not an MCP credential.
        client.post("/api/auth/setup", json={"password": PASSWORD})
        assert client.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401


def test_both_url_forms_work_without_redirect(live, token):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    for path in ("/mcp", "/mcp/"):
        reply = httpx2.post(live + path, json=INIT, headers=headers, follow_redirects=False)
        assert reply.status_code == 200, (path, reply.status_code)


def test_other_paths_under_mcp_are_not_found(live, token):
    reply = httpx2.post(live + "/mcp/x", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert reply.status_code == 404


def test_lan_host_header_is_accepted(live, token):
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}", "Host": "192.168.1.50:8642",
               "Origin": "http://192.168.1.50:8642"}
    reply = httpx2.post(live + "/mcp", json=INIT, headers=headers)
    assert reply.status_code == 200


def test_tools_listed_and_all_read_only(live, token):
    async def go():
        async with session(live, token) as client:
            return (await client.list_tools()).tools
    tools = asyncio.run(go())
    assert {t.name for t in tools} == {"get_status", "get_logs", "list_files",
                                       "get_file_details", "get_health"}


def test_get_logs_filters_and_caps(live, token, parts):
    config = parts[3]
    log = config.data_dir / "logs" / "shelfsaver.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(f"line {i} {'boom' if i % 10 == 0 else 'fine'}"
                             for i in range(5100)) + "\n", encoding="utf-8")
    assert call(live, token, "get_logs", {"lines": 3, "contains": "boom"}) == [
        "line 5070 boom", "line 5080 boom", "line 5090 boom"]
    assert len(call(live, token, "get_logs", {"lines": 999999})) == 5000
    assert len(call(live, token, "get_logs")) == 200


def add_failed_file(db, tmp_path):
    library = db.add_library("Movies", str(tmp_path / "lib"), "medium", True)
    file = db.upsert_file(library.id, str(tmp_path / "lib" / "a.mkv"), 1000, 1.0, "failed")
    run = db.start_run(file.id, 1000, "h264", 5000, "big")
    db.update_run(run.id, state="failed", error="encoder exited 1",
                  log_tail="Error while opening encoder",
                  checks_json=json.dumps({"length": True, "tracks": False}))
    return library, file


def test_details_include_failure_log(live, token, parts, tmp_path):
    library, file = add_failed_file(parts[0], tmp_path)
    details = call(live, token, "get_file_details", {"file_id": file.id})
    run = details["runs"][0]
    assert run["log_tail"] == "Error while opening encoder"
    assert run["checks"] == {"length": True, "tracks": False}
    assert run["error"] == "encoder exited 1"
    files = call(live, token, "list_files", {"status": "failed"})
    assert [f["id"] for f in files] == [file.id]
    assert call(live, token, "list_files", {"library_id": library.id + 99}) == []


def test_health_reports_encoder(live, token, parts, monkeypatch):
    def fake_run(args):
        if args[0] == "nvidia-smi":
            return "NVIDIA Test GPU, 560.94\n"
        if "-version" in args:
            return "ffmpeg version 7.1-test Copyright\n"
        return " V....D hevc_nvenc           NVIDIA NVENC hevc encoder\n"
    monkeypatch.setattr(mcp_server, "_run_tool", fake_run)
    parts[0].add_library("Gone", "Q:\\no\\such\\drive", "low", True)
    health = call(live, token, "get_health")
    assert health["gpu"] == {"available": True, "name": "NVIDIA Test GPU",
                             "driver": "560.94"}
    # The original keys are unchanged; "encoders" and "encoder" were added for pluggable encoders.
    # This fake ffmpeg lists only NVENC, so every other known encoder reports as not built in.
    assert health["ffmpeg"] == {"available": True, "version": "7.1-test", "hevc_nvenc": True,
                                "encoders": {"nvenc": True, "qsv": False}}
    assert health["encoder"] == "nvenc"
    assert health["app_version"]
    assert health["free_space"]["libraries"][0]["free_bytes"] is None
    assert health["free_space"]["scratch"]["free_bytes"] is None


def test_health_survives_missing_tools(live, token, monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_tool", lambda args: None)
    health = call(live, token, "get_health")
    assert health["gpu"]["available"] is False
    assert health["ffmpeg"]["hevc_nvenc"] is False


def test_run_tool_handles_absent_program():
    assert mcp_server._run_tool(["definitely-not-a-program-xyz"]) is None


def test_no_secret_leaks_in_status_or_health(live, token, parts, tmp_path):
    db = parts[0]
    with TestClient(create_app(*parts)) as web:
        assert web.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
        cookie = web.cookies.get("shelfsaver_session")
    add_failed_file(db, tmp_path)
    secrets_ = [db.get_setting("password_hash"), token, cookie, PASSWORD]
    assert all(secrets_)
    outputs = [json.dumps(call(live, token, name, args)) for name, args in [
        ("get_status", {}), ("get_health", {}), ("get_logs", {}), ("list_files", {}),
        ("get_file_details", {"file_id": 1})]]
    for secret in secrets_:
        assert not any(secret in out for out in outputs)


def test_health_lists_each_known_encoder_the_ffmpeg_build_has(live, token, monkeypatch):
    # An ffmpeg built with both encoders reports both, and the legacy hevc_nvenc key still works.
    def fake_run(args):
        if "-version" in args:
            return "ffmpeg version 7.1-test Copyright\n"
        if "-encoders" in args:
            return (" V....D hevc_nvenc           NVIDIA NVENC hevc encoder\n"
                    " V..... hevc_qsv             HEVC (Intel Quick Sync Video acceleration)\n")
        return None
    monkeypatch.setattr(mcp_server, "_run_tool", fake_run)
    ffmpeg = call(live, token, "get_health")["ffmpeg"]
    assert ffmpeg["encoders"] == {"nvenc": True, "qsv": True} and ffmpeg["hevc_nvenc"] is True
