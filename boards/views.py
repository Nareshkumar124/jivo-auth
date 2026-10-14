"""
The JI dashboards, inside the ticket system at /dashboards/.

* The overview and each board's page are ticket-system pages (its sidebar,
  its sign-in): the board itself is drawn in a frame, from its screen page.
* A board's **screen** page and the **carousel** are full-window pages for a
  wall: they keep their own controls and refresh in place. The screen page
  may be framed by this site only (`X-Frame-Options: SAMEORIGIN`).
* The **fragment** and **data** reads behind them answer 403 rather than
  redirect when the session has ended; the page's script then reloads,
  and the reload sends the person to the sign-in page.

Everything needs ``boards.view_dashboards`` (see ``boards.access``):
given in Administration → People and roles, or in /admin/; superusers
always have it. Anyone else gets a 403.

The pages are shells: the carousel's JavaScript fetches each board's
fragment as it comes round and again every ``refresh_seconds`` while it is
up, so the page never reloads and the rotation keeps running while a board
is being read.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_sameorigin

from . import registry
from .access import can_view_dashboards
from .compose import cached_board, render_fragment
from .sources import FactoryError, connect

logger = logging.getLogger(__name__)

DWELL_CHOICES = [15, 30, 45, 60, 90, 120, 300, 600]
DEFAULT_DWELL_SECONDS = 45
OVERSCAN_CHOICES = [0, 2, 3, 4, 5]

#: How long the list of built boards is kept. It is read for every page
#: load, and a board flagged onto the rotation can wait a minute.
BUILT_LIST_SECONDS = 60


class DashboardAccessMixin(LoginRequiredMixin):
    """Signed in through the ticket system, holding the dashboards permission."""

    login_url = reverse_lazy("support:login")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_view_dashboards(request.user):
            raise PermissionDenied

        return super().dispatch(request, *args, **kwargs)


class DashboardReadMixin(DashboardAccessMixin):
    """For the reads a page's script makes: 403, never a redirect to a login page."""

    raise_exception = True


def built_boards() -> List[Dict[str, Any]]:
    """The built boards flagged onto the rotation, from the factory database."""
    from .services.built import list_carousel_boards

    key = f"boards:built-list:{settings.FACTORY_COMPANY_CODE}"
    boards = cache.get(key)
    if boards is not None:
        return boards

    try:
        with connect() as postgres:
            boards = list_carousel_boards(postgres, settings.FACTORY_COMPANY_CODE)
    except FactoryError as exc:
        logger.warning("Built boards could not be listed: %s", exc)
        boards = []
    except Exception:  # noqa: BLE001 - a page must not 500 over the list
        logger.exception("Built boards could not be listed")
        boards = []

    cache.set(key, boards, BUILT_LIST_SECONDS)
    return boards


def slide_entry(slide: registry.Slide, slug: str | None = None, label: str | None = None) -> Dict[str, Any]:
    if slug is None:
        return {
            "key": slide.key,
            "label": label or slide.label,
            "fragmentUrl": reverse("boards:fragment", args=[slide.key]),
            "pageUrl": reverse("boards:screen", args=[slide.key]),
            "refreshSeconds": slide.refresh_seconds,
        }
    return {
        # Namespaced so a board slugged "admin" can't collide with the fixed
        # slide of that name.
        "key": f"built:{slug}",
        "label": label or slug,
        "fragmentUrl": reverse("boards:built-fragment", args=[slug]),
        "pageUrl": reverse("boards:built-screen", args=[slug]),
        "refreshSeconds": slide.refresh_seconds,
    }


def shell_context(slides: List[Dict[str, Any]], *, single: bool, title: str, embedded: bool = False) -> Dict[str, Any]:
    config = {
        "slides": slides,
        "single": single,
        "dwellChoices": DWELL_CHOICES,
        "defaultDwellSeconds": DEFAULT_DWELL_SECONDS,
        "overscanChoices": OVERSCAN_CHOICES,
        "carouselUrl": reverse("boards:carousel"),
    }
    return {
        "title": title,
        "slides": slides,
        "single": single,
        "embedded": embedded,
        "home_url": reverse("boards:index"),
        "config": config,
        "dwell_choices": DWELL_CHOICES,
        "overscan_choices": OVERSCAN_CHOICES,
        "stylesheets": registry.stylesheets(),
        "scripts": registry.scripts(),
    }


def fixed_slide_or_404(key: str) -> registry.Slide:
    slide = registry.get_slide(key)
    if slide is None:
        raise Http404("No such board.")
    return slide


def built_name(slug: str) -> str:
    return next((board["name"] for board in built_boards() if board["slug"] == slug), slug)


def portal(request, template: str, title: str, **context):
    """A page in the ticket system's layout."""
    from support.navigation import portal_context

    return render(request, template, portal_context(request, title=title, **context))


# ---- Pages in the ticket system ---------------------------------------------

@method_decorator(never_cache, name="dispatch")
class IndexView(DashboardAccessMixin, View):
    """The Dashboard section: every board, and the wall carousel."""

    def get(self, request):
        return portal(
            request,
            "boards/index.html",
            "JI dashboards",
            boards=[
                {"slide": slide, "url": reverse("boards:board", args=[slide.key]), "screen_url": reverse("boards:screen", args=[slide.key])}
                for slide in registry.SLIDES
            ],
            built=[
                {**board, "url": reverse("boards:built", args=[board["slug"]]), "screen_url": reverse("boards:built-screen", args=[board["slug"]])}
                for board in built_boards()
            ],
            company=settings.FACTORY_COMPANY_CODE,
        )


@method_decorator(never_cache, name="dispatch")
class BoardView(DashboardAccessMixin, View):
    """One board inside the ticket system's layout."""

    def get(self, request, key):
        slide = fixed_slide_or_404(key)

        return portal(
            request,
            "boards/page.html",
            slide.title,
            slide=slide,
            heading=slide.title,
            description=slide.description,
            frame_url=reverse("boards:screen", args=[key]) + "?embed=1",
            screen_url=reverse("boards:screen", args=[key]),
        )


@method_decorator(never_cache, name="dispatch")
class BuiltBoardView(DashboardAccessMixin, View):

    def get(self, request, slug):
        name = built_name(slug)

        return portal(
            request,
            "boards/page.html",
            name,
            slide=registry.BUILT,
            heading=name,
            description=registry.BUILT.description,
            frame_url=reverse("boards:built-screen", args=[slug]) + "?embed=1",
            screen_url=reverse("boards:built-screen", args=[slug]),
        )


# ---- Full-window pages for a wall ---------------------------------------------

@method_decorator(never_cache, name="dispatch")
class CarouselView(DashboardAccessMixin, View):
    """The wall rotation: the fixed boards, then the built ones."""

    def get(self, request):
        slides = [slide_entry(slide) for slide in registry.SLIDES]
        slides += [
            slide_entry(registry.BUILT, slug=board["slug"], label=board["name"])
            for board in built_boards()
        ]
        return render(
            request,
            "boards/carousel.html",
            shell_context(slides, single=False, title="Wall carousel"),
        )


@method_decorator([never_cache, xframe_options_sameorigin], name="dispatch")
class ScreenView(DashboardAccessMixin, View):
    """One fixed board on its own, refreshing in place; framed by its page."""

    def get(self, request, key):
        slide = fixed_slide_or_404(key)
        return render(
            request,
            "boards/carousel.html",
            shell_context([slide_entry(slide)], single=True, title=slide.title, embedded=request.GET.get("embed") == "1"),
        )


@method_decorator([never_cache, xframe_options_sameorigin], name="dispatch")
class BuiltScreenView(DashboardAccessMixin, View):

    def get(self, request, slug):
        name = built_name(slug)
        return render(
            request,
            "boards/carousel.html",
            shell_context(
                [slide_entry(registry.BUILT, slug=slug, label=name)],
                single=True,
                title=name,
                embedded=request.GET.get("embed") == "1",
            ),
        )


# ---- What the pages' script reads --------------------------------------------

def no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    return response


@method_decorator(never_cache, name="dispatch")
class FragmentView(DashboardReadMixin, View):
    """A board's HTML, composed now (or within BOARDS_CACHE_SECONDS)."""

    def get(self, request, key):
        slide = fixed_slide_or_404(key)
        payload = cached_board(slide)
        return no_store(HttpResponse(render_fragment(slide, payload, request)))


@method_decorator(never_cache, name="dispatch")
class BuiltFragmentView(DashboardReadMixin, View):

    def get(self, request, slug):
        payload = cached_board(registry.BUILT, slug=slug)
        return no_store(HttpResponse(render_fragment(registry.BUILT, payload, request)))


@method_decorator(never_cache, name="dispatch")
class DataView(DashboardReadMixin, View):
    """The same board as JSON, for anything that isn't this page."""

    def get(self, request, key):
        slide = fixed_slide_or_404(key)
        return no_store(JsonResponse(cached_board(slide), json_dumps_params={"ensure_ascii": False}))


@method_decorator(never_cache, name="dispatch")
class BuiltDataView(DashboardReadMixin, View):

    def get(self, request, slug):
        return no_store(
            JsonResponse(cached_board(registry.BUILT, slug=slug), json_dumps_params={"ensure_ascii": False})
        )


def as_json(value: Any) -> str:
    return json.dumps(value)
