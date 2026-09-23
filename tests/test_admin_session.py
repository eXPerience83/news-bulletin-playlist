from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie

from _http_handler import (
    HandlerCaptureMixin,
    basic_header,
    captured_response,
    initialize_handler_capture,
)

from news_bulletin_playlist.admin_session import (
    ADMIN_SESSION_ABSOLUTE_TTL,
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_IDLE_TTL,
    AdminSessionManager,
    admin_session_manager_for,
    expired_session_cookie_header,
    session_cookie_header,
)
from news_bulletin_playlist.lan_admin import LanAdminHandler, LanAdminSecurity

_PASSWORD = "long-enough-admin-password"
_SECRET = "credential-sentinel-never-render"
_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


class _Handler(HandlerCaptureMixin, LanAdminHandler):
    def __init__(
        self,
        *,
        security: LanAdminSecurity,
        path: str,
        form: dict[str, list[str]] | None = None,
        cookie: str | None = None,
    ) -> None:
        self.admin_security = security
        self.spotify_auth = None
        initialize_handler_capture(self, path=path, form=form, password=None)
        if cookie is not None:
            self.headers["Cookie"] = cookie


class _SharedRouteHandler(_Handler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path in {"/admin/sources", "/admin/diagnostics"}:
            if self._require_admin() is None:
                return
            self._reply(HTTPStatus.OK, self.path.encode(), content_type="text/plain; charset=utf-8")
            return
        super().do_GET()


def _cookie_for(session_id: str) -> str:
    return f"{ADMIN_SESSION_COOKIE}={session_id}"


def _session_id_from_set_cookie(value: str) -> str:
    cookie = SimpleCookie()
    cookie.load(value)
    return cookie[ADMIN_SESSION_COOKIE].value


def test_session_manager_enforces_idle_and_absolute_expiry() -> None:
    manager = AdminSessionManager()

    idle_session = manager.create_session(now=_NOW)
    idle_cookie = _cookie_for(idle_session)
    assert (
        manager.resolve_session(
            idle_cookie,
            now=_NOW + ADMIN_SESSION_IDLE_TTL - timedelta(seconds=1),
        )
        == idle_session
    )
    assert (
        manager.resolve_session(
            idle_cookie,
            now=_NOW + (ADMIN_SESSION_IDLE_TTL * 2),
        )
        is None
    )

    absolute_session = manager.create_session(now=_NOW)
    absolute_cookie = _cookie_for(absolute_session)
    elapsed = timedelta(minutes=20)
    while elapsed < ADMIN_SESSION_ABSOLUTE_TTL:
        assert manager.resolve_session(absolute_cookie, now=_NOW + elapsed) == absolute_session
        elapsed += timedelta(minutes=20)
    assert (
        manager.resolve_session(
            absolute_cookie,
            now=_NOW + ADMIN_SESSION_ABSOLUTE_TTL,
        )
        is None
    )


def test_csrf_is_scoped_to_one_session_and_one_use() -> None:
    manager = AdminSessionManager()
    first = manager.create_session(now=_NOW)
    second = manager.create_session(now=_NOW)

    first_token = manager.issue_csrf_token(first, now=_NOW)
    second_token = manager.issue_csrf_token(second, now=_NOW)

    assert first_token != second_token
    assert not manager.consume_csrf_token(second, first_token, now=_NOW)
    assert manager.consume_csrf_token(first, first_token, now=_NOW)
    assert not manager.consume_csrf_token(first, first_token, now=_NOW)


def test_session_cookie_attributes_follow_transport() -> None:
    http_cookie = session_cookie_header("opaque-session", secure=False)
    https_cookie = session_cookie_header("opaque-session", secure=True)
    expired_cookie = expired_session_cookie_header(secure=True)

    assert "HttpOnly" in http_cookie
    assert "SameSite=Lax" in http_cookie
    assert "Path=/admin" in http_cookie
    assert "Max-Age=" in http_cookie
    assert "Secure" not in http_cookie

    assert "HttpOnly" in https_cookie
    assert "SameSite=Lax" in https_cookie
    assert "Secure" in https_cookie

    assert "Max-Age=0" in expired_cookie
    assert "Secure" in expired_cookie


def test_anonymous_and_basic_auth_requests_use_first_party_login_boundary() -> None:
    security = LanAdminSecurity(_PASSWORD)

    anonymous = _Handler(security=security, path="/admin/")
    anonymous.do_GET()
    anonymous_response = captured_response(anonymous)
    assert anonymous_response.status == HTTPStatus.SEE_OTHER
    assert anonymous_response.extra_headers["Location"] == "/admin/login"
    assert "WWW-Authenticate" not in anonymous_response.extra_headers

    basic = _Handler(security=security, path="/admin/")
    basic.headers["Authorization"] = basic_header(_PASSWORD)
    basic.do_GET()
    basic_response = captured_response(basic)
    assert basic_response.status == HTTPStatus.SEE_OTHER
    assert basic_response.extra_headers["Location"] == "/admin/login"
    assert "WWW-Authenticate" not in basic_response.extra_headers


def test_login_page_and_generic_invalid_credentials_do_not_echo_secrets() -> None:
    security = LanAdminSecurity(_PASSWORD)

    page = _Handler(security=security, path="/admin/login")
    page.do_GET()
    page_response = captured_response(page)
    page_body = page_response.payload.decode()
    assert page_response.status == HTTPStatus.OK
    assert 'method="post" action="/admin/login"' in page_body
    assert 'name="username"' in page_body
    assert 'name="password"' in page_body
    assert "WWW-Authenticate" not in page_response.extra_headers

    invalid = _Handler(
        security=security,
        path="/admin/login",
        form={"username": ["not-admin"], "password": [_SECRET]},
    )
    invalid.do_POST()
    invalid_response = captured_response(invalid)
    invalid_body = invalid_response.payload.decode()
    assert invalid_response.status == HTTPStatus.UNAUTHORIZED
    assert "Username or password is incorrect." in invalid_body
    assert _SECRET not in invalid_body
    assert "not-admin" not in invalid_body
    assert "WWW-Authenticate" not in invalid_response.extra_headers


def test_successful_login_prevents_fixation_and_session_is_shared_across_admin_routes() -> None:
    security = LanAdminSecurity(_PASSWORD)
    manager = admin_session_manager_for(security)
    attacker_session = manager.create_session(now=_NOW)
    attacker_cookie = _cookie_for(attacker_session)

    login = _Handler(
        security=security,
        path="/admin/login",
        form={"username": ["admin"], "password": [_PASSWORD]},
        cookie=attacker_cookie,
    )
    login.do_POST()
    login_response = captured_response(login)
    assert login_response.status == HTTPStatus.SEE_OTHER
    assert login_response.extra_headers["Location"] == "/admin/"

    set_cookie = login_response.extra_headers["Set-Cookie"]
    session_id = _session_id_from_set_cookie(set_cookie)
    assert session_id != attacker_session
    assert manager.resolve_session(attacker_cookie) is None

    cookie = _cookie_for(session_id)
    for path in ("/admin/", "/admin/sources", "/admin/diagnostics"):
        handler = _SharedRouteHandler(security=security, path=path, cookie=cookie)
        handler.do_GET()
        assert captured_response(handler).status == HTTPStatus.OK


def test_logout_requires_csrf_invalidates_session_and_expires_cookie() -> None:
    security = LanAdminSecurity(_PASSWORD)
    manager = admin_session_manager_for(security)
    session_id = manager.create_session(now=_NOW)
    cookie = _cookie_for(session_id)
    csrf_token = manager.issue_csrf_token(session_id, now=_NOW)

    logout = _Handler(
        security=security,
        path="/admin/logout",
        form={"csrf_token": [csrf_token]},
        cookie=cookie,
    )
    logout.do_POST()
    response = captured_response(logout)

    assert response.status == HTTPStatus.SEE_OTHER
    assert response.extra_headers["Location"] == "/admin/login"
    assert "Max-Age=0" in response.extra_headers["Set-Cookie"]
    assert manager.resolve_session(cookie) is None

    after_logout = _Handler(security=security, path="/admin/", cookie=cookie)
    after_logout.do_GET()
    assert captured_response(after_logout).status == HTTPStatus.SEE_OTHER


def test_replacing_credential_security_object_invalidates_existing_sessions() -> None:
    previous_security = LanAdminSecurity(_PASSWORD)
    previous_manager = admin_session_manager_for(previous_security)
    session_id = previous_manager.create_session(now=_NOW)
    cookie = _cookie_for(session_id)

    rotated_security = LanAdminSecurity("different-long-enough-password")
    rotated_manager = admin_session_manager_for(rotated_security)

    assert rotated_manager.resolve_session(cookie, now=_NOW) is None
