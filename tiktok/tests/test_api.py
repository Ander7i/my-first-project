"""Тесты клиента TikTok API: токены, ошибки, пагинация, загрузка частями."""

from __future__ import annotations

from datetime import timedelta

import pytest
import requests
from conftest import NOW
from fakes import OPEN_ID, FakeResponse, FakeSession, make_tokens

from tiktok_manager.api import (
    TOKEN_PATH,
    LoginRequired,
    TikTokClient,
    TikTokError,
)
from tiktok_manager.config import DIRECT, INBOX
from tiktok_manager.db import from_iso
from tiktok_manager.video import plan_chunks

USER_INFO = "/v2/user/info/"
VIDEO_LIST = "/v2/video/list/"


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(config, db, account, session, sleeps):
    return TikTokClient(config, db, session=session, clock=lambda: NOW, sleep=sleeps.append)


def token_reply(access="access-2", refresh="refresh-2"):
    return FakeResponse(
        200,
        {
            "open_id": OPEN_ID,
            "access_token": access,
            "expires_in": 86400,
            "refresh_token": refresh,
            "refresh_expires_in": 31536000,
            "scope": "user.info.basic,video.list",
            "token_type": "Bearer",
        },
    )


def test_user_info_uses_stored_token(client, session):
    session.ok("GET", USER_INFO, {"user": {"display_name": "Канал", "follower_count": 5}})
    assert client.user_info(OPEN_ID)["follower_count"] == 5
    call = session.requests_to(USER_INFO)[0]
    assert call["headers"]["Authorization"] == "Bearer access-1"
    assert "follower_count" in call["params"]["fields"]


def test_user_info_without_stats_scope_asks_only_basic_fields(client, db, session):
    """Без user.info.stats TikTok отклонил бы запрос целиком, а не только счётчики."""
    db.save_tokens(make_tokens(NOW, scope="user.info.basic,video.list,video.upload"))
    session.ok("GET", USER_INFO, {"user": {"display_name": "Канал"}})
    client.user_info(OPEN_ID)
    assert session.requests_to(USER_INFO)[0]["params"]["fields"] == "open_id,display_name"


def test_expiring_token_is_refreshed_first(client, db, session):
    db.save_tokens(make_tokens(NOW, access_ttl=timedelta(minutes=5)))
    session.on("POST", TOKEN_PATH, token_reply())
    session.ok("GET", USER_INFO, {"user": {}})
    client.user_info(OPEN_ID)

    form = session.requests_to(TOKEN_PATH)[0]["data"]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "refresh-1"
    assert form["client_key"] == "client-key"
    assert session.requests_to(USER_INFO)[0]["headers"]["Authorization"] == "Bearer access-2"
    # TikTok может сменить refresh-токен — новый обязан сохраниться.
    account = db.account(OPEN_ID)
    assert account["refresh_token"] == "refresh-2"
    assert from_iso(account["access_expires_at"]) == NOW + timedelta(days=1)


def test_rejected_token_is_refreshed_once_and_retried(client, session):
    session.error("GET", USER_INFO, "access_token_invalid", status=401)
    session.on("POST", TOKEN_PATH, token_reply())
    session.ok("GET", USER_INFO, {"user": {"display_name": "Канал"}})
    assert client.user_info(OPEN_ID)["display_name"] == "Канал"
    assert len(session.requests_to(USER_INFO)) == 2


def test_revoked_refresh_token_requires_login(client, db, session):
    db.save_tokens(make_tokens(NOW, access_ttl=timedelta(0)))
    session.on(
        "POST",
        TOKEN_PATH,
        FakeResponse(
            400, {"error": "invalid_grant", "error_description": "Refresh token is invalid"}
        ),
    )
    with pytest.raises(LoginRequired, match="Войдите заново"):
        client.user_info(OPEN_ID)


def test_expired_refresh_token_requires_login_without_network(client, db, session):
    db.save_tokens(make_tokens(NOW, access_ttl=timedelta(0), refresh_ttl=timedelta(0)))
    with pytest.raises(LoginRequired):
        client.user_info(OPEN_ID)
    assert session.calls == []


def test_refresh_network_failure_is_retryable_not_login(client, db, session):
    db.save_tokens(make_tokens(NOW, access_ttl=timedelta(0)))
    session.on("POST", TOKEN_PATH, requests.ConnectionError("offline"))
    with pytest.raises(TikTokError) as caught:
        client.user_info(OPEN_ID)
    assert not isinstance(caught.value, LoginRequired)
    assert caught.value.retryable


def test_unknown_account_requires_login(client):
    with pytest.raises(LoginRequired):
        client.user_info("someone-else")


def test_error_in_ok_response_is_raised(client, session):
    """creator_info сообщает о лимите кодом ошибки при HTTP 200."""
    session.error("POST", "/v2/post/publish/creator_info/query/", "spam_risk_too_many_posts", 200)
    with pytest.raises(TikTokError, match="суточный лимит") as caught:
        client.creator_info(OPEN_ID)
    assert caught.value.code == "spam_risk_too_many_posts"
    assert not caught.value.retryable


def test_rate_limit_is_retryable(client, session):
    session.error("GET", USER_INFO, "rate_limit_exceeded", 429)
    with pytest.raises(TikTokError) as caught:
        client.user_info(OPEN_ID)
    assert caught.value.retryable


def test_server_error_without_json(client, session):
    session.on("GET", USER_INFO, FakeResponse(502, text="Bad Gateway"))
    with pytest.raises(TikTokError) as caught:
        client.user_info(OPEN_ID)
    assert caught.value.code == "http_502"
    assert caught.value.retryable


def test_network_error(client, session):
    session.on("GET", USER_INFO, requests.ConnectionError("offline"))
    with pytest.raises(TikTokError, match="Нет связи") as caught:
        client.user_info(OPEN_ID)
    assert caught.value.retryable


def test_videos_paginates_and_deduplicates(client, session):
    session.ok(
        "POST", VIDEO_LIST, {"videos": [{"id": "3"}, {"id": "2"}], "cursor": 100, "has_more": True}
    )
    session.ok(
        "POST", VIDEO_LIST, {"videos": [{"id": "2"}, {"id": "1"}], "cursor": 50, "has_more": False}
    )
    assert [video["id"] for video in client.videos(OPEN_ID)] == ["3", "2", "1"]
    bodies = [call["json"] for call in session.requests_to(VIDEO_LIST)]
    assert bodies == [{"max_count": 20}, {"max_count": 20, "cursor": 100}]


def test_videos_stop_when_cursor_repeats(client, session):
    session.ok("POST", VIDEO_LIST, {"videos": [{"id": "1"}], "cursor": 7, "has_more": True})
    session.ok("POST", VIDEO_LIST, {"videos": [{"id": "2"}], "cursor": 7, "has_more": True})
    assert len(client.videos(OPEN_ID)) == 2
    assert len(session.requests_to(VIDEO_LIST)) == 2


def test_init_direct_post(client, session):
    session.ok(
        "POST", "/v2/post/publish/video/init/", {"publish_id": "p1", "upload_url": "https://up"}
    )
    plan = plan_chunks(1000)
    post_info = {"title": "Текст", "privacy_level": "SELF_ONLY"}
    assert client.init_post(OPEN_ID, DIRECT, plan, post_info) == ("p1", "https://up")
    body = session.requests_to("/v2/post/publish/video/init/")[0]["json"]
    assert body == {"source_info": plan.source_info(), "post_info": post_info}


def test_init_inbox_upload_sends_no_post_info(client, session):
    session.ok(
        "POST",
        "/v2/post/publish/inbox/video/init/",
        {"publish_id": "p1", "upload_url": "https://up"},
    )
    client.init_post(OPEN_ID, INBOX, plan_chunks(1000), {"title": "не нужен"})
    body = session.requests_to("/v2/post/publish/inbox/video/init/")[0]["json"]
    assert body == {"source_info": plan_chunks(1000).source_info()}


def test_init_without_upload_url_is_error(client, session):
    session.ok("POST", "/v2/post/publish/inbox/video/init/", {"publish_id": "p1"})
    with pytest.raises(TikTokError):
        client.init_post(OPEN_ID, INBOX, plan_chunks(1000))


def test_exchange_code(client, session):
    session.on("POST", TOKEN_PATH, token_reply(access="new", refresh="r"))
    tokens = client.exchange_code("the-code", "verifier-123")
    assert (tokens.open_id, tokens.access_token, tokens.refresh_token) == (OPEN_ID, "new", "r")
    assert tokens.scopes == {"user.info.basic", "video.list"}
    form = session.requests_to(TOKEN_PATH)[0]["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["code_verifier"] == "verifier-123"
    assert form["redirect_uri"] == "http://localhost:3455/callback/"


def test_exchange_code_error(client, session):
    session.on(
        "POST",
        TOKEN_PATH,
        FakeResponse(400, {"error": "invalid_request", "error_description": "Code expired"}),
    )
    with pytest.raises(TikTokError, match="Code expired"):
        client.exchange_code("old", "v")


def test_upload_sends_chunks_in_order(client, session, tmp_path):
    path = tmp_path / "clip.mp4"
    data = bytes(range(25))
    path.write_bytes(data)
    url = "https://upload.example/video?token=1"
    session.on("PUT", url, FakeResponse(206), FakeResponse(201))
    client.upload(url, path, plan_chunks(25, chunk_size=10), "video/mp4")

    first, second = session.requests_to(url)
    assert first["headers"]["Content-Range"] == "bytes 0-9/25"
    assert first["headers"]["Content-Length"] == "10"
    assert first["data"] == data[:10]
    # Остаток уходит в последнюю часть.
    assert second["headers"]["Content-Range"] == "bytes 10-24/25"
    assert second["data"] == data[10:]
    assert second["headers"]["Content-Type"] == "video/mp4"


def test_upload_retries_server_errors(client, session, sleeps, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 5)
    url = "https://upload.example/video"
    session.on("PUT", url, FakeResponse(500), requests.ConnectionError("reset"), FakeResponse(201))
    client.upload(url, path, plan_chunks(5), "video/mp4")
    assert len(session.requests_to(url)) == 3
    assert sleeps == [2, 4]


def test_upload_expired_link_is_not_retried(client, session, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x" * 5)
    url = "https://upload.example/video"
    session.on("PUT", url, FakeResponse(403, text="expired"))
    with pytest.raises(TikTokError, match="истекла"):
        client.upload(url, path, plan_chunks(5), "video/mp4")
    assert len(session.requests_to(url)) == 1
