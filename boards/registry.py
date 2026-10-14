"""
The boards the carousel rotates, in the order the wall reads them.

Admin first because it is the summary the others drill into: somebody who
glances at the wall for four seconds should have caught the whole factory
before the screen moves on to a section of it. Built boards (the ones
people compose in the factory's dashboard builder and flag onto the
rotation) come after these, always.

Each slide names its service (``build()`` returns the board as a dict), the
template that draws that dict, and the stylesheets and scripts the carousel
page must load for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from django.utils.module_loading import import_string


@dataclass(frozen=True)
class Slide:
    #: Stable key: the URL segment, the dot's identity and the hotkey order.
    key: str
    #: What the dot says. Matches the factory sidebar's vocabulary.
    label: str
    #: The board's own heading.
    title: str
    #: Dotted path to the service class. ``Service(company_code, *, postgres,
    #: hana_for, today=None).build() -> dict``; built boards also take ``slug``.
    service: str
    #: The fragment template, rendered with ``board`` (the dict) and ``slide``.
    template: str
    #: Static paths loaded by the carousel page, after the shared sheets.
    css: Tuple[str, ...] = ()
    js: Tuple[str, ...] = ()
    #: How often a screen re-reads this board, in seconds. Served in the
    #: payload's ``meta`` so a service can slow it down.
    refresh_seconds: int = 60
    #: One line for the Dashboard section's overview page.
    description: str = ""
    #: The ticket system's icon for the board (support/partials/icons.html).
    icon: str = "chart"

    def service_class(self):
        return import_string(self.service)


SLIDES: Tuple[Slide, ...] = (
    Slide(
        key="admin",
        label="Admin Control",
        title="Admin control board",
        service="boards.services.admin.AdminBoardService",
        template="boards/fragments/admin.html",
        css=("boards/admin.css",),
        js=("boards/admin.js",),
        description="The whole factory at a glance: production, storage, dispatch, cost and the day's actions.",
    ),
    Slide(
        key="plant",
        label="Plant Control",
        title="Plant control board",
        service="boards.services.plant.PlantBoardService",
        template="boards/fragments/plant.html",
        css=("boards/plant.css",),
        js=("boards/plant.js",),
        description="Purchase orders and receipts, packaging stores, blowing, production runs, waste and the plan.",
        icon="building",
    ),
    Slide(
        key="logistics",
        label="Logistics Control",
        title="Logistics control board",
        service="boards.services.logistics.LogisticsBoardService",
        template="boards/fragments/logistics.html",
        css=("boards/logistics.css",),
        js=("boards/logistics.js",),
        description="Warehouse occupancy, non-moving stock, dispatch plans and fulfilment, freight and stock in transit.",
        icon="send",
    ),
    Slide(
        key="accounts",
        label="Accounts",
        title="Accounts board",
        service="boards.services.accounts.AccountsBoardService",
        template="boards/fragments/accounts.html",
        css=("boards/accounts.css",),
        js=("boards/accounts.js",),
        description="Imprest, cash and advances from the cash book, with the book check. Names are hidden.",
        icon="book",
    ),
)

#: A board somebody composed in the factory's dashboard builder. One slide
#: definition for every built board there will ever be: they are all the
#: same template with a different slug.
BUILT = Slide(
    key="built",
    label="Built board",
    title="Built board",
    service="boards.services.built.BuiltBoardService",
    template="boards/fragments/built.html",
    css=("boards/built.css",),
    js=("boards/built.js",),
    description="A board composed in the factory's dashboard builder.",
    icon="layers",
)


def get_slide(key: str) -> Optional[Slide]:
    for slide in SLIDES:
        if slide.key == key:
            return slide
    return None


def stylesheets() -> list:
    """Every board's stylesheets, once each, in slide order."""
    seen: list = []
    for slide in (*SLIDES, BUILT):
        for href in slide.css:
            if href not in seen:
                seen.append(href)
    return seen


def scripts() -> list:
    seen: list = []
    for slide in (*SLIDES, BUILT):
        for src in slide.js:
            if src not in seen:
                seen.append(src)
    return seen
