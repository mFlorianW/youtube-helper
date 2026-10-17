# SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>
#
# SPDX-License-Identifier: GPL-2.0-only

"""The web UI: the owner's Subscriptions, listed, added and removed in the browser, and the
status of the last cycle with whatever needs the owner's attention."""

import logging
import socket
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
from youtube_helper.cycle import BOT_CHECK_ADVICE, RETRY_PERIOD, format_size
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
    templates.env.filters["size"] = format_size
    templates.env.globals["bot_check_advice"] = BOT_CHECK_ADVICE
    templates.env.globals["retry_hours"] = RETRY_PERIOD // timedelta(hours=1)
    previews: OrderedDict[str, Channel] = OrderedDict()

    # The state store blocks while a cycle uses it, so it is only ever called off the event loop.
    def active() -> list[TrackedSubscription]:
        return [s for s in state.tracked_subscriptions() if s.active]

    def dashboard() -> dict[str, object]:
        """What every page and fragment shows: the Subscriptions with their problems, the status."""
        return {
            "subscriptions": active(),
            "outcome": state.last_cycle(),
            "problems": state.download_problems(clock()),
        }

    async def render(request: Request, template: str, **context: object) -> Response:
        return templates.TemplateResponse(
            request, template, {**await run_in_threadpool(dashboard), **context}
        )

    def is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    async def look_up(reference: str) -> Channel | str:
        """The channel `reference` leads to, or what to tell the owner if there is none."""
        try:
            return await run_in_threadpool(resolver.resolve, reference)
        except NotAChannel:
            return f"No YouTube channel found for {reference}"
        except ChannelUnavailable as error:
            return f"Can't reach YouTube right now ({error}); please try again"

    async def back_to_the_page(request: Request) -> Response:
        """The updated list for htmx, otherwise the whole page again (POST-redirect-GET)."""
        if is_htmx(request):
            return await render(request, "_subscriptions_swap.html")
        return RedirectResponse("/", status_code=303)

    async def index(request: Request) -> Response:
        return await render(request, "index.html")

    async def status(request: Request) -> Response:
        """The status with the Subscriptions' problems, as polled by the page."""
        return await render(request, "_status_poll.html")

    async def preview(request: Request) -> Response:
        reference = str((await request.form()).get("channel", "")).strip()
        context: dict[str, object] = {"reference": reference}
        channel = await look_up(reference)
        if isinstance(channel, str):
            context["error"] = channel
        else:
            previews[channel.channel_id] = channel
            previews.move_to_end(channel.channel_id)
            while len(previews) > PREVIEWS_KEPT:
                previews.popitem(last=False)
            context["channel"] = channel
            subscriptions = await run_in_threadpool(active)
            context["already"] = any(s.channel_id == channel.channel_id for s in subscriptions)
        return await render(
            request, "_preview.html" if is_htmx(request) else "index.html", **context
        )

    async def add(request: Request) -> Response:
        channel_id = str((await request.form()).get("channel_id", ""))
        channel = previews.pop(channel_id, None) or await look_up(channel_id)
        if isinstance(channel, str):
            if not is_htmx(request):
                return await render(request, "index.html", error=channel)
            # The confirm button targets the list; the error belongs where the preview was.
            response = await render(request, "_preview.html", error=channel)
            response.headers["HX-Retarget"] = "#preview"
            return response
        now = clock()
        outcome, tracked = await run_in_threadpool(state.subscribe, channel, now)
        if outcome is not Subscribed.ALREADY_ACTIVE:
            log.info(
                "%s tracking %s (%s) from %s",
                outcome.value,
                tracked.display_name,
                tracked.channel_id,
                now.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            )
        return await back_to_the_page(request)

    async def remove(request: Request) -> Response:
        removed = await run_in_threadpool(state.unsubscribe, request.path_params["channel_id"])
        if removed is not None:
            log.info(
                "Stopped tracking %s (%s): unsubscribed; its files are kept",
                removed.display_name,
                removed.channel_id,
            )
        return await back_to_the_page(request)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/status", status),
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
