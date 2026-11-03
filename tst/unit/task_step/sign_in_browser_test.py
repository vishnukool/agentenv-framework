"""sign_in_browser and the live-site marks: a deploy with a live website is marked not reproducible with its hosts
checked against the run's network allowlists, and the step hands each live site's session cookies to the browser's
browser/v1 cookies/set, through the gateway's child routes, keeping no cookie value in the context or its logs."""

from __future__ import annotations

import json
import logging
from typing import Optional

import pytest
from agentenv_protocol import client as protocol_client
from agentenv_protocol.agent_env_environment import (AgentEnvEnvironment, AgentEnvStarletteApplication,
                                                     environment_card, extension)
from agentenv_protocol.website import BROWSER_EXTENSION_URI, WebsiteEnvironment, session, website
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from agent_env.env import env as env_module
from agent_env.env.env import DeployedEnv
from agent_env.env.gateway.constants import GATEWAY_EXTENSIONS
from agent_env.env.gateway.rest_proxy import rewrite_child_card
from agent_env.task_step.context import TaskStepContext
from agent_env.task_step.registry import get_task_step_registry
from agent_env.task_step.task_steps.reset_env import ResetEnvTaskStep
from agent_env.task_step.task_steps.sign_in_browser import (LIVE_KEY, SIGN_INS_KEY, SignInBrowserTaskStep, live_sites,
                                                            mark_live)

TOKEN = "ghp_live_secret_value"


@environment_card(name="github")
@website(domains=["github.com"], live=True, allow_hosts=["*.githubusercontent.com"])
class GitHub(WebsiteEnvironment):
    @session
    def sign_in(self, user: Optional[str]) -> list[dict]:
        return [{"name": "user_session", "value": f"{TOKEN}-{user}", "secure": True, "httpOnly": True,
                 "expires": 1_900_000_000}]


@environment_card(name="notes")
@website(domains=["notes.test"])
class Notes(WebsiteEnvironment):
    pass


@environment_card(name="playwright-mcp")
class Browser(AgentEnvEnvironment):
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refuse = refuse

    @extension(BROWSER_EXTENSION_URI, op="cookies/set")
    async def _set(self, cookies: list[dict]) -> dict:
        self.calls.append(("set", {"cookies": cookies}))
        if self.refuse:
            from agentenv_protocol.errors import AgentEnvError
            raise AgentEnvError("browser_unreachable", f"refused {cookies[0]['value']}", status=502)
        return {"set": len(cookies), "open_browser": False}

    @extension(BROWSER_EXTENSION_URI, op="cookies/clear")
    async def _clear(self, domains: Optional[list[str]] = None) -> dict:
        self.calls.append(("clear", {"domains": domains}))
        return {"cleared": 0, "open_browser": False}


def _card_app(env: AgentEnvEnvironment) -> Starlette:
    app = Starlette()
    AgentEnvStarletteApplication(env._build_card(), env).add_routes_to_app(app)
    return app


class _Gateway:
    """The gateway's child routes and composed card, over the children's own apps in process."""

    def __init__(self, browser: Browser, *, live: bool = True) -> None:
        children = {"github": GitHub().create_app() if live else None, "notes": Notes().create_app(),
                    "mcp-playwright-mcp": _card_app(browser)}
        children = {key: app for key, app in children.items() if app is not None}
        self.client = TestClient(Starlette(routes=[Mount(f"/svc/{key}", app) for key, app in children.items()]))
        cards = [rewrite_child_card(key, TestClient(app).get("/.well-known/agent-env.json").json(), GATEWAY_EXTENSIONS)
                 for key, app in children.items()]
        self.card = {"name": "env", "capabilities": {"extensions": GATEWAY_EXTENSIONS},
                     "children_environments": cards}

    def deployed(self, metadata: Optional[dict] = None) -> DeployedEnv:
        return DeployedEnv(env_id="e1", env_version=1, environment_card_url="http://gw/.well-known/agent-env.json",
                           environment_card=self.card, metadata=metadata)

    async def invoke(self, base_url, card, uri, params=None, timeout=30, verify=True, *, method=None, headers=None):
        op = protocol_client.find_extension_method(card, uri, method)
        response = self.client.request(op["method"], op["endpoint"], json=params or {})
        if response.status_code >= 400:
            raise protocol_client.ExtensionCallError(response)
        return response.json()


@pytest.fixture
def browser() -> Browser:
    return Browser()


@pytest.fixture
def gateway(browser, monkeypatch) -> _Gateway:
    gateway = _Gateway(browser)
    monkeypatch.setattr(env_module.protocol_v1, "invoke_extension", gateway.invoke)
    return gateway


def test_the_step_is_registered_and_round_trips():
    step = SignInBrowserTaskStep(id="s", version=1, env_id="e1", users={"github": "octocat"}, sites=["github"])
    data = step.to_dict()
    assert data["type"] == "sign_in_browser" and get_task_step_registry()["sign_in_browser"] is SignInBrowserTaskStep
    again = SignInBrowserTaskStep.from_dict(data)
    assert (again.users, again.sites, again.browser, again.fail_task_on_error) == ({"github": "octocat"}, ["github"],
                                                                                    None, True)
    with pytest.raises(ValueError, match="maps a site"):
        SignInBrowserTaskStep(id="s", version=1, env_id="e1", users={"github": 1})


def test_only_a_live_child_is_a_live_site(gateway):
    (site,) = live_sites(gateway.card)
    assert (site.name, site.domains, site.allow_hosts, site.signs_in) == ("github", ("github.com",),
                                                                          ("*.githubusercontent.com",), True)


def test_a_deploy_with_a_live_site_is_marked_not_reproducible(gateway):
    context = TaskStepContext()
    assert mark_live(context, gateway.deployed()) == {"live": True, "reproducible": False}
    assert context.metadata["reproducible"] is False
    assert context.metadata[LIVE_KEY]["e1"] == {
        "sites": [{"name": "github", "domains": ["github.com"], "allow_hosts": ["*.githubusercontent.com"],
                   "signs_in": True}], "network": "unrestricted"}
    assert mark_live(TaskStepContext(), _Gateway(Browser(), live=False).deployed()) == {}


def test_a_computers_allowlist_that_leaves_out_a_live_host_is_recorded_and_warned(gateway, caplog):
    computer = DeployedEnv(env_id="pc", env_version=1, metadata={"computer": {"network": {
        "mode": "allowlist", "allow_hosts": ["github.com"]}}})
    context = TaskStepContext(deployed_envs=[computer])
    with caplog.at_level(logging.WARNING):
        mark_live(context, gateway.deployed())
    assert context.metadata[LIVE_KEY]["e1"]["unreachable"] == {"pc": ["*.githubusercontent.com"]}
    assert "leaves out ['*.githubusercontent.com']" in caplog.text


def test_a_computer_deployed_after_a_live_site_is_checked_too(gateway, caplog):
    context = TaskStepContext()
    site = gateway.deployed()
    mark_live(context, site)
    context.deployed_envs.append(site)
    assert context.metadata[LIVE_KEY]["e1"]["network"] == "unrestricted"
    computer = DeployedEnv(env_id="pc", env_version=1, metadata={"computer": {"network": {
        "mode": "allowlist", "allow_hosts": ["github.com"]}}})
    with caplog.at_level(logging.WARNING):
        assert mark_live(context, computer) == {}
    assert context.metadata[LIVE_KEY]["e1"]["network"] == "allowlist"
    assert context.metadata[LIVE_KEY]["e1"]["unreachable"] == {"pc": ["*.githubusercontent.com"]}
    assert "env pc's network allowlist leaves out ['*.githubusercontent.com']" in caplog.text


@pytest.mark.asyncio
async def test_the_browser_is_signed_in_with_the_live_sites_session_and_no_value_is_kept(gateway, browser, caplog):
    context = TaskStepContext(deployed_envs=[gateway.deployed()])
    step = SignInBrowserTaskStep(id="sign-in", version=1, env_id="e1", users={"github": "octocat"})
    with caplog.at_level(logging.DEBUG):
        await step.execute(context)
    assert browser.calls == [
        ("clear", {"domains": ["github.com"]}),
        ("set", {"cookies": [{"name": "user_session", "value": f"{TOKEN}-octocat", "domain": "github.com", "path": "/",
                              "secure": True, "httpOnly": True, "expires": 1_900_000_000}]})]
    record = context.metadata[SIGN_INS_KEY]["sign-in"]
    assert record["browser"] == "playwright-mcp" and record["sites"][0]["user"] == "octocat"
    assert record["sites"][0]["cookies"] == [{"name": "user_session", "domain": "github.com", "path": "/",
                                              "secure": True, "httpOnly": True, "expires": 1_900_000_000.0}]
    assert context.metadata["reproducible"] is False
    assert TOKEN not in json.dumps(context.to_safe_dict(), default=str) and TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_a_browser_that_refuses_fails_the_step_without_the_value(monkeypatch, caplog):
    browser = Browser(refuse=True)
    gateway = _Gateway(browser)
    monkeypatch.setattr(env_module.protocol_v1, "invoke_extension", gateway.invoke)
    context = TaskStepContext(deployed_envs=[gateway.deployed()])
    with pytest.raises(RuntimeError, match="HTTP 502 browser_unreachable") as e:
        await SignInBrowserTaskStep(id="s", version=1, env_id="e1").execute(context)
    assert TOKEN not in str(e.value) and TOKEN not in caplog.text
    assert TOKEN not in json.dumps(context.metadata[SIGN_INS_KEY])


@pytest.mark.asyncio
async def test_live_sites_without_a_browser_are_refused(monkeypatch, gateway):
    card = {**gateway.card, "children_environments": [c for c in gateway.card["children_environments"]
                                                      if c["name"] != "playwright-mcp"]}
    deployed = DeployedEnv(env_id="e1", env_version=1, environment_card_url="http://gw/.well-known/agent-env.json",
                           environment_card=card)
    with pytest.raises(RuntimeError, match="no child env serves urn:agentenv:browser/v1"):
        await SignInBrowserTaskStep(id="s", version=1, env_id="e1").execute(TaskStepContext(deployed_envs=[deployed]))
    step = SignInBrowserTaskStep(id="s", version=1, env_id="e1", sites=["notes"], fail_task_on_error=False)
    context = await step.execute(TaskStepContext(deployed_envs=[deployed]))
    assert "no live website named ['notes']" in context.metadata[SIGN_INS_KEY]["s"]["error"]


@pytest.mark.asyncio
async def test_a_reset_signs_the_browser_in_again(gateway, browser, monkeypatch):
    context = TaskStepContext(deployed_envs=[gateway.deployed()])
    await SignInBrowserTaskStep(id="sign-in", version=1, env_id="e1", users={"github": "octocat"}).execute(context)
    browser.calls.clear()

    class _Env:
        async def reset(self, deployed):
            return None

    monkeypatch.setattr("agent_env.env.env.Env.get", lambda *a, **k: _Env())
    await ResetEnvTaskStep(id="reset", version=1, env_id="e1").execute(context)
    assert [call for call, _ in browser.calls] == ["clear", "set"]
    assert browser.calls[1][1]["cookies"][0]["value"] == f"{TOKEN}-octocat"


@environment_card(name="github")
class GitHubMcp(AgentEnvEnvironment):
    pass


@environment_card(name="playwright-mcp")
class OtherPlaywright(AgentEnvEnvironment):
    pass


@pytest.mark.asyncio
async def test_a_site_and_a_browser_sharing_a_name_with_another_child_are_each_called_on_their_own_card(gateway,
                                                                                                         browser):
    decoys = [rewrite_child_card(key, TestClient(_card_app(env)).get("/.well-known/agent-env.json").json(),
                                 GATEWAY_EXTENSIONS)
              for key, env in (("mcp-github", GitHubMcp()), ("mcp-other-playwright", OtherPlaywright()))]
    card = {**gateway.card, "children_environments": [*decoys, *gateway.card["children_environments"]]}
    deployed = DeployedEnv(env_id="e1", env_version=1, environment_card_url="http://gw/.well-known/agent-env.json",
                           environment_card=card)
    context = TaskStepContext(deployed_envs=[deployed])
    await SignInBrowserTaskStep(id="s", version=1, env_id="e1", users={"github": "octocat"},
                                browser="playwright-mcp").execute(context)
    assert [call for call, _ in browser.calls] == ["clear", "set"]
    assert browser.calls[1][1]["cookies"][0]["value"] == f"{TOKEN}-octocat"
    assert context.metadata[SIGN_INS_KEY]["s"]["browser"] == "playwright-mcp"
