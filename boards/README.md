# boards: the JI dashboards

`/dashboards/` (the ticket system's Dashboard section) shows the factory
application's control boards and `/dashboards/carousel/` rotates them (Admin,
Plant, Logistics, Accounts, then any built boards flagged onto the rotation)
on an unattended wall screen. The boards are composed **here**, from the
factory's own databases, and drawn by Django templates; nothing calls the
factory's API.

## How a board is served

Access: `boards.access.can_view_dashboards()` (`boards.view_dashboards`, or a
superuser), checked by every view in `views.py`. The overview and each
board's page (`/dashboards/<key>/`) are ticket-system pages that frame the
board's screen page (`/dashboards/<key>/screen/?embed=1`, which may be
framed by this site only).

```text
browser (carousel.js)        views.FragmentView         compose.cached_board
  fetch /dashboards/<key>/fragment/ ──► build_board(slide) ──► Service(company, postgres=, hana_for=).build()
  every refresh_seconds           render_fragment()        └─ sections, each read on its own
```

* `registry.py` lists the slides: key, label, service class, fragment
  template, stylesheets/scripts the page must load, refresh interval.
* `compose.build_board()` opens one read-only connection to the factory's
  PostgreSQL (`sources/postgres.py`), hands it and the SAP reader factory
  (`sources/hana.py`) to the service, stamps `meta` and returns plain JSON.
  If the factory database can't be reached, the service still runs with a
  stand-in that makes every PostgreSQL section degrade with the reason.
* `compose.cached_board()` keeps the result for `BOARDS_CACHE_SECONDS`.
* `compose.render_fragment()` renders the slide's template with `board`
  (the dict), `meta` and `slide`. The carousel page swaps the fragment into
  its stage; the page itself never reloads.

## Writing a board service

```python
from boards import formatting
from boards.sections import SectionBuilder

class ExampleBoardService(SectionBuilder):
    REFRESH_SECONDS = 60

    def __init__(self, company_code, *, postgres, hana_for, today=None):
        self.company_code = company_code
        self.postgres = postgres          # .rows(sql, params) / .one() / .value(); %s placeholders
        self.hana = hana_for(company_code)  # .rows(sql, params) / .one(); ? placeholders; .table("OINM")
        self.today = today or formatting.today()   # the factory's local date
        self._init_sections()

    def build(self):
        return {
            "cost": self.section("cost", self._cost, needs_sap=False),
            "output": self.section("output", self._output, needs_factory=False),
            "meta": {**self.section_meta(), "refresh_seconds": self.REFRESH_SECONDS},
        }
```

Rules the factory's boards keep, kept here:

* **A section that cannot be read is `None`**, named in `meta.degraded` with
  its reason in `meta.reasons`. Never `{}`, never zero. `section()` catches
  everything; a SAP connection failure latches SAP off for the rest of the
  build, a factory-database failure does the same for PostgreSQL sections.
  A source that isn't configured raises `SAPNotConfigured` /
  `FactoryNotConfigured`, so the board says "not configured", not "did
  not answer".
* **`needs_sap` / `needs_factory`** say what a section reads, so a latch
  skips only the sections it affects.
* **Payloads are plain JSON**: `formatting.jsonable()` converts Decimals and
  dates, but services should build with floats and ISO strings anyway.
* **"Today" is the factory's day** (`BOARDS_TIME_ZONE`), never UTC.
* **Tons = litres / 1000** (`formatting.tons`), the business's rule.
* **HANA SQL** must qualify tables with `self.hana.table("OINM")` and use
  `?` placeholders; PostgreSQL SQL uses `%s`. `sources.hana.LITRES_PER_UNIT`
  is the litre gate (expects `M` = OITM).
* **Names are masked** unless a board has a reason to show them: a wall
  screen in a corridor must not name who holds cash.

## Writing a fragment template

`templates/boards/fragments/<key>.html` receives `board`, `meta` and
`slide`. It renders a whole `.ops-board` element (the shared stylesheet,
`static/boards/ops-board.css`, is the factory's; every length is
`calc(N * var(--u))` so the board scales with the screen), ends with
`{% include "boards/fragments/_meta.html" %}`, and uses the filters in
`templatetags/boards.py` (`whole`, `qty`, `money`, `dec`, `pct`, `signed`,
`day_label`, `share`, `get`, `clock`). A `None` section renders an
`.ops-unread` tile, not a zero. An element with `data-clock` is kept ticking
by the page. Per-board CSS goes in `static/boards/<key>.css`, listed on the
slide; optional JS in `static/boards/<key>.js` listens for the
`boards:mounted` event (`event.detail.key`, `event.detail.stage`).

## Built boards

`services/built.py` reads a board composed in the factory's dashboard
builder (its grid and placements) and runs each card from the catalogue in
`services/built_cards/`. A new card is one module there: compute the figure,
return one of the shapes in `built_cards/viz.py`, call `catalogue.register`.
`built_cards.viz.present()` precomputes the geometry the template draws.

## Testing

`testing.py` has `FakePostgres` / `FakeHana` (answer SQL by substring or
regex), `HanaDown` and `hana_factory()`. Services are tested with those;
no test may open a real connection. The shell's tests switch both sources
off with `override_settings`.

## Settings

`FACTORY_DB_*`, `FACTORY_HANA_*`, `FACTORY_COMPANY_CODE`, `BOARDS_TIME_ZONE`,
`BOARDS_CACHE_SECONDS`: see `.env.example` and `docs/board-carousel.md`.
