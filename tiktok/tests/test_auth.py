"""Тесты входа: PKCE по правилам TikTok, разбор адреса возврата, локальный сервер."""

from __future__ import annotations

import re
import socket
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import NOW
from fakes import OPEN_ID, make_tokens

from tiktok_manager.auth import (
    AuthError,
    CallbackServer,
    authorize_url,
    login,
    make_challenge,
    make_verifier,
    parse_callback,
    scopes_for,
)
from tiktok_manager.config import DIRECT, INBOX


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_verifier_fits_pkce_rules():
    verifier = make_verifier()
    assert 43 <= len(verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9\-._~]+", verifier)
    assert make_verifier() != verifier


def test_challenge_is_hex_sha256():
    """TikTok для десктопа ждёт hex, а не base64url из RFC 7636."""
    assert make_challenge("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_scopes_follow_publish_mode():
    assert "video.publish" in scopes_for(DIRECT)
    assert "video.upload" not in scopes_for(DIRECT)
    assert "video.upload" in scopes_for(INBOX)
    assert {"user.info.basic", "user.info.stats", "video.list"} <= set(scopes_for(INBOX))


def test_authorize_url(config):
    url = authorize_url(config, state="st", challenge="ch", scopes=("a.b", "c.d"))
    parts = urlsplit(url)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}"
        == "https://www.tiktok.com/v2/auth/authorize/"
    )
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert query == {
        "client_key": "client-key",
        "response_type": "code",
        "scope": "a.b,c.d",
        "redirect_uri": "http://localhost:3455/callback/",
        "state": "st",
        "code_challenge": "ch",
        "code_challenge_method": "S256",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3455/callback/?code=abc%2Adef&scopes=x&state=st",
        "/callback/?code=abc%2Adef&state=st",
        "code=abc%2Adef&state=st",
        "  http://localhost:3455/callback/?state=st&code=abc*def  ",
    ],
)
def test_parse_callback_decodes_code(url):
    assert parse_callback(url, state="st") == "abc*def"


def test_parse_callback_error_from_tiktok():
    url = (
        "http://localhost:3455/callback/?error=access_denied&error_description=User+denied&state=st"
    )
    with pytest.raises(AuthError, match="User denied"):
        parse_callback(url, state="st")


def test_parse_callback_rejects_foreign_state():
    with pytest.raises(AuthError, match="другому запросу"):
        parse_callback("/callback/?code=abc&state=other", state="st")


def test_parse_callback_requires_code():
    with pytest.raises(AuthError, match="нет кода"):
        parse_callback("/callback/?state=st", state="st")


def test_callback_server_catches_redirect():
    port = free_port()
    replies = []

    def browser():
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)
        except urllib.error.HTTPError as exc:
            replies.append(exc.code)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/callback/?code=xyz&state=st", timeout=5
        ) as response:
            replies.append(response.read().decode("utf-8"))

    with CallbackServer(f"http://localhost:{port}/callback/") as server:
        thread = threading.Thread(target=browser)
        thread.start()
        path = server.wait(timeout=10)
        thread.join(timeout=10)

    assert parse_callback(path, state="st") == "xyz"
    assert replies[0] == 404
    assert "Готово" in replies[1]


def test_callback_server_busy_port_suggests_paste():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        with pytest.raises(AuthError, match="--paste"):
            CallbackServer(f"http://localhost:{port}/callback/")


def test_callback_server_timeout():
    with CallbackServer(f"http://localhost:{free_port()}/callback/") as server:
        with pytest.raises(AuthError, match="не завершён"):
            server.wait(timeout=0)


class LoginClient:
    def __init__(self, scope: str) -> None:
        self.scope = scope
        self.exchanged: list[tuple[str, str]] = []

    def exchange_code(self, code, verifier):
        self.exchanged.append((code, verifier))
        return make_tokens(NOW, scope=self.scope)

    def user_info(self, open_id):
        return {"display_name": "Мой канал"}


def run_paste_login(config, db, client):
    printed = []

    def ask(prompt):
        url = next(line for line in printed if line.startswith("https://"))
        state = parse_qs(urlsplit(url).query)["state"][0]
        return f"http://localhost:3455/callback/?code=the-code&state={state}"

    result = login(config, client, db, paste=True, ask=ask, say=printed.append)
    return result, printed


def test_paste_login_saves_account(config, db):
    client = LoginClient(scope="user.info.basic,user.info.stats,video.list,video.upload")
    result, _ = run_paste_login(config, db, client)
    assert result.open_id == OPEN_ID
    assert result.display_name == "Мой канал"
    assert result.missing_scopes == ()
    code, verifier = client.exchanged[0]
    assert code == "the-code"
    assert len(verifier) >= 43
    assert db.account(OPEN_ID)["display_name"] == "Мой канал"


def test_login_reports_missing_scopes(config, db):
    client = LoginClient(scope="user.info.basic,video.list")
    result, _ = run_paste_login(config, db, client)
    assert result.missing_scopes == ("user.info.stats", "video.upload")


def test_login_needs_app_keys(config, db):
    from dataclasses import replace

    with pytest.raises(RuntimeError, match="TIKTOK_CLIENT_KEY"):
        login(replace(config, client_key=""), LoginClient(""), db, paste=True)
