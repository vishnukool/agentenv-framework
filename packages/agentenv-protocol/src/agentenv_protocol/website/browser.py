"""``urn:agentenv:browser/v1``: what a browser env offers agentenv's steps and graders, never its agent. ``cookies/set``
adds cookies to the browser, so its pages start signed in to a site, and ``cookies/clear`` removes them, all of them or
those of some domains. A cookie is ``{name, value, domain, path, secure, httpOnly, expires, sameSite}``, ``expires``
in epoch seconds and absent for a cookie that lasts the browser's session, as a site's ``session`` returns it.

A cookie's value is a credential: ``redacted`` is what a log or a record keeps of one, and the errors here name a
cookie by its position and field, never by what it holds."""
from __future__ import annotations

from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

BROWSER_EXTENSION_URI = "urn:agentenv:browser/v1"
SET_COOKIES = "cookies/set"
CLEAR_COOKIES = "cookies/clear"


class Cookie(BaseModel):
    """One cookie as a browser adds it, for ``domain`` and ``path``; a leading dot on ``domain`` covers its
    subdomains too."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: str
    domain: str = Field(min_length=1)
    path: str = "/"
    secure: bool = False
    httpOnly: bool = False
    expires: Optional[float] = None
    sameSite: Optional[Literal["Strict", "Lax", "None"]] = None

    def key(self) -> tuple[str, str, str]:
        """What a browser keeps one cookie under: a later one with the same key replaces it."""
        return self.name, self.domain.lower(), self.path

    def matches(self, domains: Iterable[str]) -> bool:
        """Whether the cookie is for one of ``domains`` or a subdomain of one."""
        return domain_matches(self.domain, domains)


def domain_matches(domain: str, domains: Iterable[str]) -> bool:
    """Whether a cookie's ``domain`` is one of ``domains`` or a subdomain of one, leading dots aside."""
    host = domain.lstrip(".").lower()
    return any(host == d or host.endswith("." + d) for d in (d.lstrip(".").lower() for d in domains))


def cookies(raw: Any, default_domain: Optional[str] = None) -> list[Cookie]:
    """``raw``, a list of cookie objects, as Cookies; one without a domain takes ``default_domain``. Raises ValueError
    naming each problem by the cookie's index and field, never by a value."""
    if not isinstance(raw, list):
        raise ValueError("cookies is a list of cookie objects")
    parsed, problems = [], []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"cookies[{i}] is not an object")
            continue
        if not item.get("domain") and default_domain:
            item = {**item, "domain": default_domain}
        try:
            parsed.append(Cookie.model_validate(item))
        except ValidationError as e:
            problems += [f"cookies[{i}].{'.'.join(map(str, err['loc'])) or 'cookie'}: {err['msg']}" for err in e.errors()]
    if problems:
        raise ValueError("; ".join(problems))
    return parsed


def redacted(given: Iterable[Cookie]) -> list[dict]:
    """What a record keeps of each cookie: everything but its value."""
    return [c.model_dump(exclude={"value"}, exclude_none=True) for c in given]


def storage_state(given: Iterable[Cookie]) -> list[dict]:
    """The cookies as a Playwright storage state file lists them: ``expires`` -1 for a session cookie."""
    return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
             "expires": c.expires if c.expires is not None else -1, "httpOnly": c.httpOnly, "secure": c.secure,
             "sameSite": c.sameSite or "Lax"} for c in given]


def devtools(given: Iterable[Cookie]) -> list[dict]:
    """The cookies as Chrome DevTools' ``Storage.setCookies`` takes them."""
    out = []
    for c in given:
        param = {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure,
                 "httpOnly": c.httpOnly}
        if c.expires is not None:
            param["expires"] = c.expires
        if c.sameSite is not None:
            param["sameSite"] = c.sameSite
        out.append(param)
    return out
