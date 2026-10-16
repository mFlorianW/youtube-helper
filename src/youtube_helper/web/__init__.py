# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The web UI: the owner's Subscriptions, listed, added and removed in the browser."""

import logging
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from youtube_helper.channels import Channel, ChannelResolver, ChannelUnavailable, NotAChannel
from youtube_helper.state import StateStore, Subscribed, TrackedSubscription

log = logging.getLogger(__name__)

HERE = Path(__file__).parent

# Previews remembered so that confirming one needs no second lookup.
PREVIEWS_KEPT = 32


def create_app(
    state: StateStore,
    resolver: ChannelResolver,
    *,
    timezone: ZoneInfo,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Starlette:
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["local_time"] = lambda moment: moment.astimezone(timezone).strftime(
        "%Y-%m-%d %H:%M"
    )
    previews: OrderedDict[str, Channel] = OrderedDict()

    def active() -> list[TrackedSubscription]:
        return [s for s in state.tracked_subscriptions() if s.active]

    def render(request: Request, template: str, **context: object) -> Response:
        return templates.TemplateResponse(request, template, {"subscriptions": active(), **context})

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    def back_to_the_page(request: Request) -> Response:
        """The updated list for htmx, otherwise the whole page again (POST-redirect-GET)."""
        if is_htmx(request):
            return render(request, "_subscriptions_swap.html")
        return RedirectResponse("/", status_code=303)

    async def index(request: Request) -> Response:
        return render(request, "index.html")

    async def preview(request: Request) -> Response:
        reference = str((await request.form()).get("channel", "")).strip()
        context: dict[str, object] = {"reference": reference}
        try:
            channel = await run_in_threadpool(resolver.resolve, reference)
        except NotAChannel:
            context["error"] = f"No YouTube channel found for {reference}"
        except ChannelUnavailable as error:
            context["error"] = f"Can't reach YouTube right now ({error}); please try again"
        else:
            previews[channel.channel_id] = channel
            previews.move_to_end(channel.channel_id)
            while len(previews) > PREVIEWS_KEPT:
                previews.popitem(last=False)
            context["channel"] = channel
            context["already"] = any(s.channel_id == channel.channel_id for s in active())
        return render(request, "_preview.html" if is_htmx(request) else "index.html", **context)

    async def add(request: Request) -> Response:
        channel_id = str((await request.form()).get("channel_id", ""))
        channel = previews.pop(channel_id, None)
        if channel is None:
            try:
                channel = await run_in_threadpool(resolver.resolve, channel_id)
            except (NotAChannel, ChannelUnavailable) as error:
                return render(request, "index.html", error=str(error))
        now = clock()
        outcome, tracked = state.subscribe(channel, now)
        if outcome is not Subscribed.ALREADY_ACTIVE:
            log.info(
                "%s tracking %s (%s) from %s",
                outcome.value,
                tracked.display_name,
                tracked.channel_id,
                now.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            )
        return back_to_the_page(request)

    async def remove(request: Request) -> Response:
        removed = state.unsubscribe(request.path_params["channel_id"])
        if removed is not None:
            log.info(
                "Stopped tracking %s (%s): unsubscribed; its files are kept",
                removed.display_name,
                removed.channel_id,
            )
        return back_to_the_page(request)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/preview", preview, methods=["POST"]),
            Route("/subscriptions", add, methods=["POST"]),
            Route("/subscriptions/{channel_id}/remove", remove, methods=["POST"]),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]
    )


def serve(app: Starlette, port: int) -> None:
    """Serve `app` on `port` in the background, for as long as the process runs.

    Raises OSError right away if the port can't be bound.
    """
    if socket.has_dualstack_ipv6():
        sock = socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True)
    else:
        sock = socket.create_server(("", port))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="web", daemon=True).start()
    log.info("Web UI listening on port %d", port)
