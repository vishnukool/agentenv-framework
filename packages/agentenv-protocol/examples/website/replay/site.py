"""Widget Docs, replayed: the pages of a captured docs site, served as they were captured for any host, with the
newsletter sign-up the capture can't answer written as ``@api`` routes on top. The sign-ups are the site's state: the
data plane exports them, and ``/go`` shows what a run changed."""
from __future__ import annotations

from pydantic import BaseModel

from agentenv_protocol import environment_card
from agentenv_protocol.website import BadRequest, Conflict, api, website
from agentenv_protocol.website.replay import ReplayWebsite


class Subscription(BaseModel):
    email: str


@environment_card(name="widget-docs")
@website(domains=["docs.widgets.test"])
class WidgetDocs(ReplayWebsite):
    capture = "capture.warc.gz"

    @api.post("/api/subscribe", status=201)
    def subscribe(self, body: Subscription) -> dict:
        """Sign an email address up for the newsletter."""
        if "@" not in body.email:
            raise BadRequest("that is not an email address")
        if self.store.find("subscriptions", email=body.email):
            raise Conflict(f"{body.email} is subscribed already")
        return self.store.insert("subscriptions", {"email": body.email, "at": self.now().isoformat()})

    @api.get("/api/subscriptions")
    def subscriptions(self) -> list[dict]:
        """Everyone signed up."""
        return self.store["subscriptions"]


if __name__ == "__main__":
    WidgetDocs().serve()
