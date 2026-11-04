"""Live websites and browser/v1's cookies: a live site serves none of its pages, declares live and allow_hosts on its
card, and its @session method's cookies come back from website/v1's session checked and given its domain; cookies are
checked without naming their values, and kept in a record without them."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from agentenv_protocol import DataPart, add_data, get_data, reset_data
from agentenv_protocol.client import find_extension
from agentenv_protocol.website import (WEBSITE_EXTENSION_URI, Cookie, WebsiteEnvironment, api, ready_check, session,
                                       website)
from agentenv_protocol.website.browser import cookies, devtools, redacted, storage_state

SESSION = "/agentenv/ext/website/session"


@website(domains=["github.com"], live=True, allow_hosts=["*.githubusercontent.com", "api.github.com"])
class GitHub(WebsiteEnvironment):
    """A live site: its data hooks would call GitHub's API with the token setting."""

    def __init__(self) -> None:
        super().__init__()
        self.repos: list = []

    @ready_check
    def has_token(self) -> bool:
        return bool(self.setting("GITHUB_TOKEN"))

    @session
    async def sign_in(self, user: str | None) -> list[dict]:
        return [{"name": "user_session", "value": f"{self.setting('GITHUB_TOKEN')}:{user}", "secure": True,
                 "httpOnly": True, "expires": 1_900_000_000},
                {"name": "logged_in", "value": "yes", "domain": ".github.com"}]

    @reset_data
    async def reset(self) -> None:
        self.repos = []

    @add_data
    async def load(self, parts: list) -> None:
        self.repos += [repo for part in parts for repo in part.data["repos"]]

    @get_data
    async def export(self) -> list:
        return [DataPart(data={"repos": self.repos})]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    return TestClient(GitHub().create_app())


def test_a_live_site_serves_no_pages_only_its_health_data_plane_and_card(client):
    for path in ("/", "/login", "/api/session"):
        assert client.get(path).status_code == 404, path
    assert client.get("/api/health").status_code == 200
    card = client.get("/.well-known/agent-env.json").json()
    params = find_extension(card, WEBSITE_EXTENSION_URI)["params"]
    assert params["live"] is True and params["signs_in"] is True
    assert params["domains"] == ["github.com"]
    assert params["allow_hosts"] == ["*.githubusercontent.com", "api.github.com"]
    assert "session_cookie" not in params


def test_a_live_sites_health_says_whether_its_account_works(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert TestClient(GitHub().create_app()).get("/api/health").status_code == 503


def test_the_session_operation_returns_the_session_methods_cookies_for_the_sites_domain(client):
    answer = client.post(SESSION, json={"user": "octocat"}).json()
    assert answer["user"] == "octocat"
    assert answer["cookies"] == [
        {"name": "user_session", "value": "ghp_secret:octocat", "domain": "github.com", "path": "/", "secure": True,
         "httpOnly": True, "expires": 1_900_000_000},
        {"name": "logged_in", "value": "yes", "domain": ".github.com", "path": "/", "secure": False,
         "httpOnly": False}]
    assert answer["expires_at"] == "2030-03-17T17:46:40Z"


def test_a_live_site_without_a_session_method_says_so():
    @website(domains=["example.com"], live=True)
    class Plain(WebsiteEnvironment):
        pass

    client = TestClient(Plain().create_app())
    params = find_extension(client.get("/.well-known/agent-env.json").json(), WEBSITE_EXTENSION_URI)["params"]
    assert params["signs_in"] is False
    answer = client.post(SESSION, json={})
    assert answer.status_code == 501 and answer.json()["error"]["code"] == "no_session"


def test_a_session_method_returning_a_bad_cookie_is_refused_without_its_value():
    @website(domains=["example.com"], live=True)
    class Broken(WebsiteEnvironment):
        @session
        def sign_in(self, user):
            return {"cookies": [{"name": "", "value": "hunter2"}], "expires_at": None}

    answer = TestClient(Broken().create_app()).post(SESSION, json={})
    assert answer.status_code == 500 and "cookies[0].name" in answer.text and "hunter2" not in answer.text


def test_a_synthetic_site_keeps_its_signed_session_and_may_override_it():
    @website(domains=["shop.example"])
    class Shop(WebsiteEnvironment):
        pass

    @website(domains=["persona.example"])
    class Persona(WebsiteEnvironment):
        @session
        def as_persona(self, user):
            return [{"name": "persona", "value": user or "guest"}]

    signed = TestClient(Shop().create_app()).post(SESSION, json={"user": "ann"}).json()
    assert signed["cookies"][0]["name"] == "agentenv_session_Shop"
    persona = TestClient(Persona().create_app()).post(SESSION, json={}).json()
    assert persona["cookies"] == [{"name": "persona", "value": "guest", "domain": "persona.example", "path": "/",
                                   "secure": False, "httpOnly": False}]


@pytest.mark.parametrize("declared, problem", [
    (dict(live=True), "names its domains"),
    (dict(domains=["a.example"], live=True, static="dist"), "takes no static"),
    (dict(domains=["a.example"], allow_hosts=["cdn.example"]), "allow_hosts is for a live site"),
    (dict(domains=["https://a.example"]), "not a host name"),
    (dict(domains=["a.example"], live=True, allow_hosts=["*"]), "not a host name"),
    (dict(domains="a.example"), "not the string"),
])
def test_a_live_declaration_is_checked(declared, problem):
    with pytest.raises(ValueError, match=problem):
        website(**declared)


def test_a_live_site_serves_no_api():
    with pytest.raises(ValueError, match="serves no @api route"):
        @website(domains=["a.example"], live=True)
        class WithApi(WebsiteEnvironment):
            @api.get("/api/x")
            def x(self) -> dict:
                return {}


def test_cookies_are_checked_by_index_and_field_never_by_value():
    with pytest.raises(ValueError) as e:
        cookies([{"name": "a", "value": "s3cret", "domain": "a.example", "sameSite": "sometimes"}, "x",
                 {"name": "b", "value": "s3cret"}])
    message = str(e.value)
    assert "cookies[0].sameSite" in message and "cookies[1] is not an object" in message
    assert "cookies[2].domain" in message and "s3cret" not in message
    with pytest.raises(ValueError, match="list"):
        cookies({"name": "a"})


def test_a_cookie_is_kept_without_its_value_and_given_to_a_browser_in_its_own_shape():
    given = cookies([{"name": "sid", "value": "v", "secure": True}, {"name": "t", "value": "w", "expires": 5,
                                                                      "sameSite": "Strict"}],
                    default_domain="a.example")
    assert redacted(given) == [{"name": "sid", "domain": "a.example", "path": "/", "secure": True, "httpOnly": False},
                               {"name": "t", "domain": "a.example", "path": "/", "secure": False, "httpOnly": False,
                                "expires": 5.0, "sameSite": "Strict"}]
    assert storage_state(given)[0] == {"name": "sid", "value": "v", "domain": "a.example", "path": "/",
                                       "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"}
    assert "expires" not in devtools(given)[0] and devtools(given)[1]["sameSite"] == "Strict"
    assert Cookie(name="a", value="", domain=".A.example").matches(["a.example"])
    assert Cookie(name="a", value="", domain="sub.a.example").matches(["a.example"])
    assert not Cookie(name="a", value="", domain="nota.example").matches(["a.example"])
    for domain in ("app.example.com", ".example.com", "deep.app.example.com"):
        assert Cookie(name="a", value="", domain=domain).matches(["*.example.com"]), domain
    assert not Cookie(name="a", value="", domain="badexample.com").matches(["*.example.com"])


def test_a_website_browser_declares_the_browser_v1_whose_cookies_sign_in_browser_sets():
    """A WebsiteBrowserEnvironment's card and the cookie operations name one extension, so a browser that adds them
    is the one sign_in_browser finds."""
    from agentenv_protocol.website import BROWSER_EXTENSION_URI
    from agentenv_protocol.website_browser import BROWSER

    assert BROWSER == BROWSER_EXTENSION_URI
