from __future__ import annotations

from http import HTTPStatus

import pytest
from _html import assert_valid_admin_html, assert_valid_document_html
from _http_handler import (
    HandlerCaptureMixin,
    captured_response,
    initialize_handler_capture,
)

from news_bulletin_playlist.diagnostics_runtime import _safe_operational_status_page
from news_bulletin_playlist.engine import OperationalStatus
from news_bulletin_playlist.lan_admin import LanAdminHandler, LanAdminSecurity
from news_bulletin_playlist.runtime import AdminSecurity
from news_bulletin_playlist.spotify.auth import AuthorizationState

_PASSWORD = "long-enough-admin-password"
_SECRET = "login-secret-sentinel"
_LOGIN_CSRF_EXEMPT_ACTIONS = frozenset({"/admin/login"})


class _LoginHandler(HandlerCaptureMixin, LanAdminHandler):
    def __init__(
        self,
        *,
        security: AdminSecurity,
        path: str = "/admin/login",
        form: dict[str, list[str]] | None = None,
    ) -> None:
        self.admin_security = security
        self.spotify_auth = None
        initialize_handler_capture(self, path=path, form=form, password=None)


def _document_with_form(*, action: str, csrf: bool = False) -> str:
    csrf_input = '<input type="hidden" name="csrf_token" value="token">' if csrf else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Audit fixture</title>
</head>
<body>
  <h1>Audit fixture</h1>
  <form method="post" action="{action}">{csrf_input}</form>
</body>
</html>
"""


def _assert_login_contract(body: str) -> None:
    assert_valid_document_html(
        body,
        csrf_exempt_post_actions=_LOGIN_CSRF_EXEMPT_ACTIONS,
    )
    assert 'method="post" action="/admin/login"' in body
    assert '<label for="admin-username">Username</label>' in body
    assert 'id="admin-username"' in body
    assert 'name="username"' in body
    assert 'autocomplete="username"' in body
    assert '<label for="admin-password">Password</label>' in body
    assert 'id="admin-password"' in body
    assert 'name="password"' in body
    assert 'type="password"' in body
    assert 'autocomplete="current-password"' in body
    assert 'name="csrf_token"' not in body


def test_admin_login_get_rendered_document_contract() -> None:
    handler = _LoginHandler(security=AdminSecurity(_PASSWORD))
    handler.do_GET()
    response = captured_response(handler)
    body = response.payload.decode()

    assert response.status == HTTPStatus.OK
    _assert_login_contract(body)
    assert "Username or password is incorrect." not in body
    assert "Trusted LAN mode." not in body


def test_admin_login_invalid_credentials_rendered_document_contract() -> None:
    handler = _LoginHandler(
        security=AdminSecurity(_PASSWORD),
        form={"username": ["submitted-user"], "password": [_SECRET]},
    )
    handler.do_POST()
    response = captured_response(handler)
    body = response.payload.decode()

    assert response.status == HTTPStatus.UNAUTHORIZED
    _assert_login_contract(body)
    assert "Username or password is incorrect." in body
    assert "submitted-user" not in body
    assert _SECRET not in body


def test_admin_login_lan_warning_rendered_document_contract() -> None:
    handler = _LoginHandler(security=LanAdminSecurity(_PASSWORD))
    handler.do_GET()
    response = captured_response(handler)
    body = response.payload.decode()

    assert response.status == HTTPStatus.OK
    _assert_login_contract(body)
    assert "Trusted LAN mode." in body


def test_login_csrf_exemption_is_action_specific() -> None:
    login = _document_with_form(action="/admin/login")
    assert_valid_document_html(
        login,
        csrf_exempt_post_actions=_LOGIN_CSRF_EXEMPT_ACTIONS,
    )

    with pytest.raises(AssertionError, match="POST form is missing csrf_token: /admin/login"):
        assert_valid_document_html(login)

    arbitrary_post = _document_with_form(action="/admin/arbitrary")
    with pytest.raises(AssertionError, match="POST form is missing csrf_token: /admin/arbitrary"):
        assert_valid_document_html(
            arbitrary_post,
            csrf_exempt_post_actions=_LOGIN_CSRF_EXEMPT_ACTIONS,
        )


def test_protocol_relative_form_action_is_rejected() -> None:
    document = _document_with_form(action="//attacker.example/collect", csrf=True)

    with pytest.raises(
        AssertionError,
        match=r"form action must remain same-origin: //attacker\.example/collect",
    ):
        assert_valid_document_html(document)


@pytest.mark.parametrize(
    "action",
    [
        r"/\attacker.example/collect",
        "/\t/attacker.example/collect",
    ],
)
def test_browser_normalized_external_form_actions_are_rejected(action: str) -> None:
    document = _document_with_form(action=action, csrf=True)

    with pytest.raises(AssertionError, match="form action must remain same-origin"):
        assert_valid_document_html(document)


def test_base_element_is_rejected() -> None:
    document = _document_with_form(action="/admin/mutate", csrf=True).replace(
        "<head>",
        '<head><base href="https://attacker.example/">',
    )

    for assertion in (assert_valid_document_html, assert_valid_admin_html):
        with pytest.raises(AssertionError, match=r"<base> is not allowed"):
            assertion(document)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("formaction", "/admin/alternate"),
        ("formmethod", "post"),
        ("formenctype", "application/x-www-form-urlencoded"),
        ("formtarget", "_self"),
        ("formnovalidate", "formnovalidate"),
    ],
)
def test_submitter_form_overrides_are_rejected(attribute: str, value: str) -> None:
    document = _document_with_form(action="/admin/mutate", csrf=True).replace(
        "</form>",
        f'<button type="submit" {attribute}="{value}">Submit</button></form>',
    )

    with pytest.raises(AssertionError, match=f"{attribute} is not allowed"):
        assert_valid_document_html(document)


def test_formaction_cannot_escape_login_csrf_exemption() -> None:
    document = _document_with_form(action="/admin/login").replace(
        "</form>",
        '<button type="submit" formaction="/admin/arbitrary">Submit</button></form>',
    )

    with pytest.raises(AssertionError, match="formaction is not allowed"):
        assert_valid_document_html(
            document,
            csrf_exempt_post_actions=_LOGIN_CSRF_EXEMPT_ACTIONS,
        )


def test_formmethod_cannot_create_unchecked_post() -> None:
    document = (
        _document_with_form(action="/admin/arbitrary")
        .replace('method="post"', 'method="get"')
        .replace(
            "</form>",
            '<button type="submit" formmethod="post">Submit</button></form>',
        )
    )

    with pytest.raises(AssertionError, match="formmethod is not allowed"):
        assert_valid_document_html(document)


@pytest.mark.parametrize(
    "csrf_input",
    [
        '<input name="csrf_token" value="token">',
        '<input type="hidden" name="csrf_token" value="">',
        '<input type="hidden" name="csrf_token" value="token" disabled>',
        '<input type="hidden" name="csrf_token" value="token" form="other-form">',
    ],
)
def test_ineffective_csrf_controls_are_rejected(csrf_input: str) -> None:
    document = _document_with_form(action="/admin/mutate").replace(
        "</form>",
        f"{csrf_input}</form>",
    )

    with pytest.raises(
        AssertionError,
        match="csrf_token must be a non-empty enabled hidden input",
    ):
        assert_valid_document_html(document)


def test_duplicate_csrf_controls_are_rejected() -> None:
    document = _document_with_form(action="/admin/mutate", csrf=True).replace(
        "</form>",
        '<input type="hidden" name="csrf_token" value="second-token"></form>',
    )

    with pytest.raises(AssertionError, match="multiple csrf_token inputs"):
        assert_valid_document_html(document)


def test_disabled_fieldset_cannot_hide_ineffective_csrf_control() -> None:
    document = _document_with_form(action="/admin/mutate").replace(
        "</form>",
        '<fieldset disabled><input type="hidden" name="csrf_token" value="token"></fieldset></form>',
    )

    with pytest.raises(AssertionError, match=r"disabled <fieldset> is not allowed"):
        assert_valid_document_html(document)


def test_post_with_csrf_remains_valid_by_default() -> None:
    assert_valid_document_html(
        _document_with_form(action="/admin/mutate", csrf=True),
    )


def test_public_status_rendered_document_contract() -> None:
    body = _safe_operational_status_page(
        ready=True,
        spotify_state=AuthorizationState.CONNECTED,
        status=OperationalStatus(configured=True),
        build_revision="html-contract-test",
    ).decode()

    assert_valid_document_html(body)
