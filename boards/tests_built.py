"""
Tests for the built boards: the ones people compose in the factory's
dashboard builder and flag onto the wall rotation.

Nothing here reaches the factory database or SAP. The service takes its
sources as arguments and every test hands it the fakes in ``boards.testing``,
with every card's rows written out so the figures can be checked by hand.

What is pinned, and why these: the geometry rules (a retired card is named,
a card that no longer fits is dropped, the first card placed wins a square),
option coercion falling back rather than refusing a board, every card's
figure against hand-computed values, one failing card costing one tile, a
SAP outage degrading only the cards that read SAP, and the template drawing
each shape with the classes the shared stylesheet styles.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from . import formatting, registry
from .services.built import (
    BOARD_SQL,
    CAROUSEL_SQL,
    PLACEMENTS_SQL,
    BuiltBoardService,
    list_carousel_boards,
)
from .services.built_cards import catalogue, viz
from .services.built_cards.catalogue import CardContext, CardOption, CardSpec
from .services.built_cards.constants import CARD_ACCENTS, GRID_MAX_COLUMNS, WALL_MAX_ROWS
from .services.built_cards.dispatch import STAGE_LEGS_SQL
from .services.built_cards.gate import ON_SITE_SQL, TURNAROUND_SQL
from .services.built_cards.production import DOWNTIME_SQL
from .sources import FactoryQueryError, FactoryUnavailable, Unavailable
from .testing import FakeHana, FakePostgres, HanaDown, hana_factory

#: A Tuesday. The turnaround card picks today's weekday out of its chart.
TODAY = date(2026, 10, 13)
COMPANY = "JIVO_OIL"

#: The factory's catalogue, by key. A placement stores the key, so these
#: must stay exactly what the factory registers.
FACTORY_KEYS = {
    "section_label",
    "dispatch_stage_clock",
    "production_line_downtime",
    "gate_truck_turnaround",
    "gate_trucks_on_site",
}

# Needles: a distinctive substring of each statement, so a fake can answer
# the board, its placements and each card's read separately.
BOARD_NEEDLE = "b.slug = %s"
PLACEMENTS_NEEDLE = "FROM board_builder_boardplacement"
CAROUSEL_NEEDLE = "b.in_carousel = TRUE"
DISPATCH_NEEDLE = "FROM gate_core_salesdispatchgateout"
TURNAROUND_NEEDLE = "a.status = 'DEPARTED'"
ON_SITE_NEEDLE = "a.status IN ('INSIDE', 'LOADING')"
DOWNTIME_NEEDLE = "FROM production_execution_productionrun"


def board_row(**overrides) -> Dict[str, Any]:
    row = {
        "id": 7,
        "slug": "night-shift",
        "name": "Night shift",
        "description": "The lines, after six",
        "mode": "WALL",
        "columns": 4,
        "rows": 3,
        "surface": "light",
        "density": "normal",
        "accent": "store",
        "show_heading": True,
        "visibility": "PUBLISHED",
        "in_carousel": True,
    }
    row.update(overrides)
    return row


def placement(card_key: str, column: int, row: int, **overrides) -> Dict[str, Any]:
    entry = {
        "id": 100 + column + row * 10,
        "card_key": card_key,
        "column": column,
        "row": row,
        "title": "",
        "accent": "",
        "options": {},
    }
    entry.update(overrides)
    return entry


#: Every card's rows, chosen so the figures can be computed by hand.
DISPATCH_ROWS = [
    {"load_seconds": 3600, "gate_seconds": 1800},   # 1h / 0.5h
    {"load_seconds": 7200, "gate_seconds": 3600},   # 2h / 1h
    {"load_seconds": -10, "gate_seconds": 100},     # corrected by hand: dropped
]
TURNAROUND_ROWS = [
    {"gate_in_date": date(2026, 10, 12), "hours": 2.0},   # Mon
    {"gate_in_date": date(2026, 10, 13), "hours": 3.0},   # Tue (today)
    {"gate_in_date": date(2026, 10, 6), "hours": 5.0},    # Tue
    {"gate_in_date": date(2026, 10, 7), "hours": -1.0},   # Wed, negative: dropped
    {"gate_in_date": date(2026, 10, 8), "hours": 10.0},   # Thu
]
ON_SITE_ROWS = [
    {"gate_in_date": date(2026, 10, 13), "status": "INSIDE"},
    {"gate_in_date": date(2026, 10, 12), "status": "LOADING"},
    {"gate_in_date": date(2026, 10, 5), "status": "INSIDE"},   # 8 days: stale
]
DOWNTIME_ROWS = [
    {"line_name": "Sidel", "runs": 2, "breakdown": 400},
    {"line_name": "Line A", "runs": 4, "breakdown": 40},
    {"line_name": None, "runs": 2, "breakdown": None},
]


def factory(board: Optional[Dict[str, Any]] = None, placements: Optional[List[Dict[str, Any]]] = None) -> FakePostgres:
    """A factory database answering the board, its placements and every
    card's read."""
    fake = FakePostgres()
    if board is not None:
        fake.add(BOARD_NEEDLE, [board])
    fake.add(PLACEMENTS_NEEDLE, placements or [])
    fake.add(DISPATCH_NEEDLE, DISPATCH_ROWS)
    fake.add(TURNAROUND_NEEDLE, TURNAROUND_ROWS)
    fake.add(ON_SITE_NEEDLE, ON_SITE_ROWS)
    fake.add(DOWNTIME_NEEDLE, DOWNTIME_ROWS)
    return fake


def replace(fake: FakePostgres, needle: str, rows: List[Dict[str, Any]]) -> FakePostgres:
    """Swap one read's answer: the fake answers with the first match."""
    fake.answers = [(other, answer) for other, answer in fake.answers if other != needle]
    return fake.add(needle, rows)


def build(postgres, *, slug: str = "night-shift", hana=None, today: date = TODAY) -> Dict[str, Any]:
    service = BuiltBoardService(
        COMPANY,
        slug=slug,
        postgres=postgres,
        hana_for=hana_factory(hana or FakeHana()),
        today=today,
    )
    return formatting.jsonable(service.build())


def cards_by_key(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {card["card_key"]: card for card in payload["cards"]}


class RaisingPostgres(FakePostgres):
    """A factory database that rejects one statement."""

    def __init__(self, needle: str, error: Exception, answers=None):
        super().__init__(answers)
        self.needle = needle
        self.error = error

    def rows(self, sql, params=None):
        if self.needle in sql:
            self.calls.append((sql, params))
            raise self.error
        return super().rows(sql, params)


class CountingHanaDown(HanaDown):
    """SAP that never answers, counting how often it was asked."""

    def __init__(self):
        super().__init__(COMPANY)
        self.asked = 0

    def table(self, name):
        self.asked += 1
        return super().table(name)


def _sap_rows(context: CardContext) -> dict:
    rows = context.hana.rows(f'SELECT COUNT(*) AS "n" FROM {context.hana.table("OINM")}')
    return viz.figure(value=str(rows[0]["n"]), unit="rows", viz=viz.nothing())


def sap_spec(key: str = "test_sap_rows") -> CardSpec:
    return CardSpec(
        key=key,
        title="SAP rows",
        summary="Reads SAP, for the latch tests.",
        category="Test",
        build=_sap_rows,
        needs_sap=True,
        needs_factory=False,
    )


# ---------------------------------------------------------------------------


class CatalogueTests(SimpleTestCase):

    def test_the_factorys_cards_are_all_registered_under_their_keys(self):
        self.assertEqual({spec.key for spec in catalogue.all_cards()}, FACTORY_KEYS)
        for key in FACTORY_KEYS:
            self.assertIsNotNone(catalogue.get(key), key)

    def test_every_card_fits_a_wall_and_wears_a_known_hue(self):
        for spec in catalogue.all_cards():
            with self.subTest(card=spec.key):
                self.assertTrue(1 <= spec.columns <= GRID_MAX_COLUMNS)
                self.assertTrue(1 <= spec.rows <= WALL_MAX_ROWS)
                self.assertIn(spec.accent, CARD_ACCENTS)
                self.assertTrue(spec.title and spec.summary and spec.category)

    def test_the_footprints_are_the_factorys(self):
        sizes = {spec.key: (spec.columns, spec.rows) for spec in catalogue.all_cards()}
        self.assertEqual(
            sizes,
            {
                "section_label": (1, 1),
                "dispatch_stage_clock": (2, 1),
                "production_line_downtime": (2, 2),
                "gate_truck_turnaround": (2, 1),
                "gate_trucks_on_site": (1, 1),
            },
        )

    def test_only_the_section_label_holds_no_feed(self):
        feedless = [spec.key for spec in catalogue.all_cards() if spec.feed is None]
        self.assertEqual(feedless, ["section_label"])

    def test_a_mistaken_spec_fails_at_import_rather_than_on_a_tile(self):
        with self.assertRaises(ValueError):
            CardSpec(key="x", title="x", summary="", category="T", build=_sap_rows, accent="red")
        with self.assertRaises(ValueError):
            CardSpec(key="x", title="x", summary="", category="T", build=_sap_rows, columns=GRID_MAX_COLUMNS + 1)
        with self.assertRaises(ValueError):
            CardSpec(key="x", title="x", summary="", category="T", build=_sap_rows, rows=WALL_MAX_ROWS + 1)

    def test_a_key_cannot_be_registered_twice(self):
        with self.assertRaises(ValueError):
            catalogue.register(catalogue.require("section_label"))

    def test_temporarily_registers_and_forgets(self):
        with catalogue.temporarily(sap_spec("test_temp")):
            self.assertIsNotNone(catalogue.get("test_temp"))
        self.assertIsNone(catalogue.get("test_temp"))
        with self.assertRaises(KeyError):
            catalogue.require("test_temp")

    def test_categories_follow_the_palette_order(self):
        self.assertEqual(catalogue.categories(), ("Dispatch", "Gate", "Layout", "Production"))


class CardOptionTests(SimpleTestCase):

    def test_an_unknown_choice_falls_back_rather_than_raising(self):
        option = CardOption(key="w", label="W", kind="choice", default="30", choices=(("7", "7"), ("30", "30")))
        self.assertEqual(option.coerce("14"), "30")
        self.assertEqual(option.coerce(None), "30")
        self.assertEqual(option.coerce("7"), "7")

    def test_integers_are_clamped_to_their_bounds(self):
        option = CardOption(key="n", label="N", kind="int", default=4, minimum=2, maximum=8)
        self.assertEqual(option.coerce(50), 8)
        self.assertEqual(option.coerce(1), 2)
        self.assertEqual(option.coerce("6"), 6)
        self.assertEqual(option.coerce("x"), 4)

    def test_switches_and_undeclared_options(self):
        option = CardOption(key="b", label="B", kind="bool", default=False)
        self.assertIs(option.coerce(1), True)
        spec = catalogue.require("production_line_downtime")
        self.assertEqual(
            spec.coerce_options({"lines": "3", "stale_key": 1}),
            {"window_days": "30", "lines": 3},
        )


class VizTests(SimpleTestCase):

    def test_a_card_must_return_a_value_or_a_reason(self):
        with self.assertRaises(ValueError):
            viz.validate(viz.figure(value=""))
        viz.validate(viz.missing("no rate configured"))
        viz.validate(viz.figure(value="1", viz=viz.nothing()))

    def test_an_unknown_shape_is_refused(self):
        with self.assertRaises(ValueError):
            viz.validate(viz.figure(value="1", viz={"kind": "pie"}))

    def test_missing_is_not_a_zero(self):
        payload = viz.missing("Nobody configured this", sub="per litre")
        self.assertEqual(payload["value"], "")
        self.assertEqual(payload["missing"], "Nobody configured this")
        self.assertIsNone(payload["viz"])

    def test_compact_formats_like_the_factorys_renderer(self):
        self.assertEqual(viz.compact(1234), "1,234")
        self.assertEqual(viz.compact(123456), "1,23,456")
        self.assertEqual(viz.compact(12.345), "12.3")
        self.assertEqual(viz.compact(1.5), "1.50")
        self.assertEqual(viz.compact(float("nan")), "—")
        self.assertEqual(viz.compact("x"), "—")

    def test_meter_shares_are_of_the_total(self):
        shown = viz.present(viz.meter([("Filled", 30, "main"), ("Empty", 10, "mute"), ("None", 0, "light")]))
        self.assertEqual([seg["pct"] for seg in shown["segments"]], [75.0, 25.0, 0.0])
        self.assertEqual([seg["figure"] for seg in shown["segments"]], ["30.0", "10.0", "0.00"])
        self.assertEqual([seg["sliver"] for seg in shown["segments"]], [True, True, False])
        self.assertEqual([seg["delay"] for seg in shown["segments"]], ["", "0.1", "0.2"])
        # The factory's keys survive beside the geometry.
        self.assertEqual(shown["segments"][0]["value"], 30.0)

    def test_pair_rows_share_one_scale(self):
        shown = viz.present(viz.pair([("A", 1.5, "main"), ("B", 0.75, "light")], note="n"))
        self.assertEqual([row["pct"] for row in shown["rows"]], [100.0, 50.0])
        self.assertEqual([row["figure"] for row in shown["rows"]], ["1.50", "0.75"])

    def test_bars_keep_a_floor_for_small_days_and_none_for_empty_ones(self):
        shown = viz.present(viz.bars([("Mon", 0.1, False), ("Tue", 0.0, True), ("Wed", 100.0, False)]))
        days = shown["days"]
        self.assertEqual(days[0]["height"], 2.0)
        self.assertEqual(days[0]["figure"], "0.10")
        self.assertEqual(days[1]["height"], 0.0)
        self.assertEqual(days[1]["figure"], "")
        self.assertTrue(days[1]["current"])
        self.assertEqual(days[2]["height"], 100.0)
        self.assertEqual(days[2]["figure"], "100")

    def test_table_header_and_body_carry_their_alignment(self):
        shown = viz.present(viz.table(columns=["Line", "Runs"], rows=[["A", "1"]], aligns=["left", "right"]))
        self.assertEqual(shown["header"], [{"label": "Line", "align": "left"}, {"label": "Runs", "align": "right"}])
        self.assertEqual(shown["body"], [[{"text": "A", "align": "left"}, {"text": "1", "align": "right"}]])
        bare = viz.present(viz.table(columns=["X"], rows=[["1"]]))
        self.assertEqual(bare["header"][0]["align"], "left")

    def test_a_missing_tile_never_wears_a_condition_colour(self):
        payload = viz.missing("gone")
        payload["tag"] = viz.tag("late", "bad")
        self.assertEqual(viz.present_card(payload)["tag"]["tone"], "nil")
        self.assertIsNone(viz.present_card(viz.figure(value="1"))["viz"])

    def test_every_kind_the_service_can_produce_is_drawn_by_the_template(self):
        shapes = {
            "meter": (viz.meter([("A", 1, "main")]), "ops-meter"),
            "bars": (viz.bars([("Mon", 1, True)]), "ops-bars"),
            "pair": (viz.pair([("A", 1, "main")]), "ops-pair"),
            "matrix": (viz.matrix(columns=["GRPO"], rows=[("0–14", [3]), ("15+", [None])], caption="c"), "cbx-matrix"),
            "table": (viz.table(columns=["A"], rows=[["1"]]), "cbx-table"),
            "split": (viz.split([("Inside", "2")]), "cbx-split"),
            "none": (viz.nothing(), "ops-viz"),
        }
        self.assertEqual(set(shapes), set(viz.VIZ_KINDS))
        for kind, (shape, marker) in shapes.items():
            with self.subTest(kind=kind):
                html = render_card(viz.figure(value="1", viz=shape))
                self.assertIn(marker, html)


def render_card(payload: Optional[Dict[str, Any]], **card_overrides) -> str:
    """One card on a one-cell board, drawn."""
    shown = viz.present_card(payload) if payload is not None else None
    card = {
        "id": 1,
        "card_key": "gate_trucks_on_site",
        "title": "Trucks on site",
        "accent": "transport",
        "column": 0,
        "row": 0,
        "columns": 1,
        "rows": 1,
        "category": "Gate",
        "note": "",
        "payload": shown,
    }
    card.update(card_overrides)
    board = {key: value for key, value in board_row(columns=1, rows=1).items() if key != "id"}
    meta = {
        "degraded": [] if payload is not None else [card["card_key"]],
        "reasons": {} if payload is not None else {card["card_key"]: "SAP HANA could not be reached."},
        "warnings": [],
        "withheld": [],
        "retired": [],
        "misplaced": [],
        "generated_at": "2026-10-13T09:05:00+05:30",
        "refresh_seconds": 60,
        "slug": board["slug"],
        "company": COMPANY,
    }
    payload_ = {"board": board, "cards": [card], "meta": meta}
    return render_to_string("boards/fragments/built.html", {"board": payload_, "meta": meta, "slide": registry.BUILT})


# ---------------------------------------------------------------------------


class CarouselListTests(SimpleTestCase):

    def test_lists_the_published_wall_boards_on_the_rotation(self):
        fake = FakePostgres().add(CAROUSEL_NEEDLE, [
            {"slug": "night-shift", "name": "Night shift"},
            {"slug": "quality", "name": "Quality wall"},
        ])

        boards = list_carousel_boards(fake, COMPANY)

        self.assertEqual(boards, [
            {"slug": "night-shift", "name": "Night shift"},
            {"slug": "quality", "name": "Quality wall"},
        ])
        sql, params = fake.calls[0]
        self.assertEqual(params, [COMPANY, "PUBLISHED"])
        # The rotation's rules are in the statement, not left to the caller.
        for clause in ("b.mode = 'WALL'", "b.in_carousel = TRUE", "b.is_active = TRUE", "b.visibility = %s", "ORDER BY b.name"):
            self.assertIn(clause, sql)
        self.assertIs(sql, CAROUSEL_SQL)

    def test_an_empty_rotation_is_an_empty_list(self):
        self.assertEqual(list_carousel_boards(FakePostgres(), COMPANY), [])


class ServiceShapeTests(SimpleTestCase):

    def test_the_payload_is_the_factorys(self):
        payload = build(factory(board_row(), [placement("section_label", 0, 0, title="Gate")]))

        self.assertEqual(
            payload["board"],
            {
                "slug": "night-shift",
                "name": "Night shift",
                "description": "The lines, after six",
                "mode": "WALL",
                "columns": 4,
                "rows": 3,
                "surface": "light",
                "density": "normal",
                "accent": "store",
                "show_heading": True,
                "visibility": "PUBLISHED",
                "in_carousel": True,
            },
        )
        self.assertEqual(
            set(payload["cards"][0]),
            {"id", "card_key", "title", "accent", "column", "row", "columns", "rows", "category", "note", "payload"},
        )
        meta = payload["meta"]
        for key in ("degraded", "reasons", "warnings", "withheld", "retired", "misplaced", "generated_at", "refresh_seconds", "slug"):
            self.assertIn(key, meta)
        self.assertEqual(meta["refresh_seconds"], 60)
        self.assertEqual(meta["slug"], "night-shift")
        self.assertEqual(meta["withheld"], [])
        self.assertEqual(meta["degraded"], [])

    def test_the_payload_is_plain_json(self):
        fake = factory(board_row(), [
            placement("dispatch_stage_clock", 0, 0),
            placement("gate_truck_turnaround", 2, 0),
            placement("gate_trucks_on_site", 0, 1),
            placement("production_line_downtime", 1, 1),
        ])
        payload = build(fake)
        json.dumps(payload)
        self.assertEqual(len(payload["cards"]), 4)

    def test_the_board_is_read_published_only_for_this_company(self):
        fake = factory(board_row(), [])
        build(fake)

        sql, params = fake.calls[0]
        self.assertIs(sql, BOARD_SQL)
        self.assertEqual(params, [COMPANY, "night-shift", "PUBLISHED"])
        sql, params = fake.calls[1]
        self.assertIs(sql, PLACEMENTS_SQL)
        self.assertEqual(params, [7])
        self.assertIn('ORDER BY p."row", p."column"', sql)

    def test_a_cards_title_and_accent_come_from_the_placement_then_the_board(self):
        payload = build(factory(board_row(accent="store"), [
            placement("section_label", 0, 0),
            placement("section_label", 1, 0, title="Gate", accent="dispatch"),
        ]))

        first, second = payload["cards"]
        self.assertEqual((first["title"], first["accent"]), ("Untitled section", "store"))
        self.assertEqual((second["title"], second["accent"]), ("Gate", "dispatch"))
        self.assertEqual(first["payload"]["value"], " ")
        self.assertEqual(first["payload"]["viz"], {"kind": "none"})
        self.assertEqual(first["category"], "Layout")

    def test_options_arrive_as_text_from_some_drivers(self):
        payload = build(factory(board_row(), [
            placement("dispatch_stage_clock", 0, 0, options='{"window_days": "7"}'),
        ]))
        self.assertIn("last 7 days", payload["cards"][0]["payload"]["sub"])

    def test_today_sets_the_clock_every_card_reads(self):
        fake = factory(board_row(), [
            placement("dispatch_stage_clock", 0, 0),
            placement("gate_truck_turnaround", 2, 0),
            placement("production_line_downtime", 0, 1),
        ])
        service = BuiltBoardService(COMPANY, slug="night-shift", postgres=fake, hana_for=hana_factory(FakeHana()), today=TODAY)
        service.build()

        self.assertEqual(service.today, TODAY)
        since_by_sql = {sql: params for sql, params in fake.calls}
        self.assertEqual(since_by_sql[STAGE_LEGS_SQL][0], COMPANY)
        self.assertEqual(since_by_sql[STAGE_LEGS_SQL][1].date(), TODAY - timedelta(days=30))
        self.assertEqual(since_by_sql[TURNAROUND_SQL], [TODAY - timedelta(days=30)])
        self.assertEqual(since_by_sql[DOWNTIME_SQL], [COMPANY, TODAY - timedelta(days=30)])


class GeometryTests(SimpleTestCase):

    def test_a_retired_card_is_named_rather_than_drawn(self):
        payload = build(factory(board_row(), [placement("card_that_was_deleted", 0, 0)]))

        self.assertEqual(payload["meta"]["retired"], ["card_that_was_deleted"])
        self.assertEqual(payload["cards"], [])
        self.assertTrue(any("no longer exist" in warning for warning in payload["meta"]["warnings"]))

    def test_a_card_hanging_off_the_edge_is_dropped(self):
        # A 2x1 card in the last of four columns, and a 2x2 card in the last of three rows.
        payload = build(factory(board_row(), [
            placement("gate_truck_turnaround", 3, 0),
            placement("production_line_downtime", 0, 2),
        ]))

        self.assertEqual(payload["meta"]["misplaced"], ["gate_truck_turnaround", "production_line_downtime"])
        self.assertEqual(payload["cards"], [])
        self.assertTrue(any("no longer fit" in warning for warning in payload["meta"]["warnings"]))

    def test_a_card_that_no_longer_fits_on_a_shrunken_board_is_dropped(self):
        payload = build(factory(board_row(columns=1, rows=1), [placement("gate_truck_turnaround", 0, 0)]))

        self.assertEqual(payload["meta"]["misplaced"], ["gate_truck_turnaround"])
        self.assertEqual(payload["cards"], [])

    def test_the_first_card_placed_wins_a_square(self):
        # The turnaround card covers (0,0) and (1,0); the tile dropped on (1,0) loses.
        payload = build(factory(board_row(), [
            placement("gate_truck_turnaround", 0, 0),
            placement("gate_trucks_on_site", 1, 0),
            placement("gate_trucks_on_site", 2, 0),
        ]))

        self.assertEqual(payload["meta"]["misplaced"], ["gate_trucks_on_site"])
        self.assertEqual([(card["card_key"], card["column"]) for card in payload["cards"]],
                         [("gate_truck_turnaround", 0), ("gate_trucks_on_site", 2)])

    def test_a_tall_card_takes_its_whole_footprint(self):
        # Downtime is 2x2 at (0,0): (1,1) is taken, (2,1) is free.
        payload = build(factory(board_row(), [
            placement("production_line_downtime", 0, 0),
            placement("gate_trucks_on_site", 1, 1),
            placement("gate_trucks_on_site", 2, 1),
        ]))

        self.assertEqual(payload["meta"]["misplaced"], ["gate_trucks_on_site"])
        self.assertEqual([card["column"] for card in payload["cards"]], [0, 2])
        self.assertEqual((payload["cards"][0]["columns"], payload["cards"][0]["rows"]), (2, 2))


class OptionTests(SimpleTestCase):

    def test_an_unknown_window_falls_back_to_the_default(self):
        payload = build(factory(board_row(), [
            placement("dispatch_stage_clock", 0, 0, options={"window_days": "14"}),
            placement("gate_truck_turnaround", 2, 0, options={"window_days": "90"}),
        ]))
        cards = cards_by_key(payload)
        self.assertIn("last 30 days", cards["dispatch_stage_clock"]["payload"]["sub"])
        self.assertIn("last 90 days", cards["gate_truck_turnaround"]["payload"]["sub"])

    def test_the_lines_shown_are_clamped_and_defaulted(self):
        ten_lines = [{"line_name": f"L{index}", "runs": 1, "breakdown": index} for index in range(10)]
        fake = factory(board_row(), [
            placement("production_line_downtime", 0, 0, options={"lines": 50}),
            placement("production_line_downtime", 2, 0, options={"lines": "x"}),
        ])
        replace(fake, DOWNTIME_NEEDLE, ten_lines)

        payload = build(fake)

        clamped, defaulted = payload["cards"]
        self.assertEqual(len(clamped["payload"]["viz"]["rows"]), 8)
        self.assertEqual(len(defaulted["payload"]["viz"]["rows"]), 4)


class CardFigureTests(SimpleTestCase):

    def test_dispatch_stage_clock(self):
        payload = build(factory(board_row(), [placement("dispatch_stage_clock", 0, 0)]))
        card = payload["cards"][0]["payload"]

        # Legs 1h+2h and 0.5h+1h over two dockings; the corrected one is dropped.
        self.assertEqual(card["value"], "2.25")
        self.assertEqual(card["unit"], "h average")
        self.assertEqual(card["sub"], "2 dockings · last 30 days")
        self.assertEqual(card["tag"], {"label": "67% loading", "tone": "warn"})
        self.assertIsNone(card["missing"])
        shape = card["viz"]
        self.assertEqual(shape["kind"], "pair")
        self.assertEqual(
            [(row["label"], row["value"], row["fill"], row["pct"], row["figure"]) for row in shape["rows"]],
            [("Dock → print", 1.5, "main", 100.0, "1.50"), ("Print → out", 0.75, "light", 50.0, "0.75")],
        )
        self.assertEqual(shape["note"], "Average hours per leg, one scale")

    def test_dispatch_stage_clock_with_a_balanced_clock_is_neutral(self):
        fake = factory(board_row(), [placement("dispatch_stage_clock", 0, 0)])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != DISPATCH_NEEDLE]
        fake.add(DISPATCH_NEEDLE, [{"load_seconds": 3600, "gate_seconds": 3600}])

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["value"], "2.00")
        self.assertEqual(card["tag"], {"label": "50% loading", "tone": "neut"})

    def test_dispatch_stage_clock_with_nothing_in_the_window(self):
        fake = factory(board_row(), [placement("dispatch_stage_clock", 0, 0, options={"window_days": "7"})])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != DISPATCH_NEEDLE]

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["missing"], "No docking completed both stages in the last 7 days.")
        self.assertEqual(card["sub"], "Docked to printed to gone")
        self.assertEqual(card["value"], "")
        self.assertIsNone(card["viz"])

    def test_gate_truck_turnaround(self):
        payload = build(factory(board_row(), [placement("gate_truck_turnaround", 0, 0)]))
        card = payload["cards"][0]["payload"]

        # Hours [2, 3, 5, 10]: median 4, nearest-rank p90 is the 10-hour trip.
        self.assertEqual(card["value"], "4.00")
        self.assertEqual(card["unit"], "h median")
        self.assertEqual(card["sub"], "4 trips · last 30 days · every company")
        self.assertEqual(card["tag"], {"label": "p90 10.00 h", "tone": "warn"})
        self.assertEqual(card["note"], "Median hours on site by weekday. Today is picked out.")
        days = {day["label"]: day for day in card["viz"]["days"]}
        self.assertEqual(card["viz"]["kind"], "bars")
        self.assertEqual([day["label"] for day in card["viz"]["days"]], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        # Tuesday's median of 3 and 5 is 4; Thursday's single 10-hour trip tops the chart.
        self.assertEqual((days["Mon"]["value"], days["Mon"]["height"]), (2.0, 20.0))
        self.assertEqual((days["Tue"]["value"], days["Tue"]["height"], days["Tue"]["current"]), (4.0, 40.0, True))
        self.assertEqual((days["Thu"]["value"], days["Thu"]["height"]), (10.0, 100.0))
        self.assertEqual((days["Wed"]["value"], days["Wed"]["height"], days["Wed"]["figure"]), (0.0, 0.0, ""))
        self.assertEqual([day["current"] for day in card["viz"]["days"]].count(True), 1)

    def test_gate_truck_turnaround_short_trips_are_neutral(self):
        fake = factory(board_row(), [placement("gate_truck_turnaround", 0, 0)])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != TURNAROUND_NEEDLE]
        fake.add(TURNAROUND_NEEDLE, [{"gate_in_date": "2026-10-12", "hours": "1.5"}, {"gate_in_date": "2026-10-12", "hours": None}])

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["value"], "1.50")
        self.assertEqual(card["sub"], "1 trips · last 30 days · every company")
        self.assertEqual(card["tag"]["tone"], "neut")

    def test_gate_truck_turnaround_with_no_trips(self):
        fake = factory(board_row(), [placement("gate_truck_turnaround", 0, 0)])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != TURNAROUND_NEEDLE]

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["missing"], "No truck completed a trip in the last 30 days.")
        self.assertEqual(card["sub"], "Gate to gate, every company")

    def test_gate_trucks_on_site(self):
        payload = build(factory(board_row(), [placement("gate_trucks_on_site", 0, 0)]))
        card = payload["cards"][0]["payload"]

        self.assertEqual(card["value"], "3")
        self.assertEqual(card["unit"], "on site")
        self.assertEqual(card["sub"], "Oldest 8 days · every company")
        self.assertEqual(card["tag"], {"label": "1 stale", "tone": "bad"})
        self.assertEqual(card["viz"], {
            "kind": "split",
            "parts": [{"label": "Inside", "value": "2"}, {"label": "Loading", "value": "1"}, {"label": "2 days+", "value": "1"}],
        })

    def test_gate_trucks_on_site_tones(self):
        def tone_for(rows):
            fake = factory(board_row(), [placement("gate_trucks_on_site", 0, 0)])
            fake.answers = [(needle, answer) for needle, answer in fake.answers if needle != ON_SITE_NEEDLE]
            fake.add(ON_SITE_NEEDLE, rows)
            return build(fake)["cards"][0]["payload"]

        fresh = tone_for([{"gate_in_date": TODAY, "status": "LOADING"}])
        self.assertEqual(fresh["tag"], {"label": "all fresh", "tone": "ok"})
        self.assertEqual(fresh["sub"], "Oldest 0 days · every company")
        overnight = tone_for([{"gate_in_date": TODAY - timedelta(days=1), "status": "INSIDE"}])
        self.assertEqual(overnight["sub"], "Oldest 1 day · every company")
        self.assertEqual(overnight["tag"]["tone"], "ok")
        stale = tone_for([{"gate_in_date": TODAY - timedelta(days=3), "status": "INSIDE"}])
        self.assertEqual(stale["tag"], {"label": "1 stale", "tone": "warn"})

    def test_gate_trucks_on_site_with_nothing_open(self):
        fake = factory(board_row(), [placement("gate_trucks_on_site", 0, 0)])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != ON_SITE_NEEDLE]

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["value"], "0")
        self.assertEqual(card["sub"], "Nothing open at the gate")
        self.assertIsNone(card["tag"])
        self.assertEqual(card["viz"], {"kind": "none"})
        self.assertIsNone(card["missing"])

    def test_production_line_downtime(self):
        payload = build(factory(board_row(), [placement("production_line_downtime", 0, 0)]))
        card = payload["cards"][0]["payload"]

        # 440 breakdown minutes over 8 runs; Sidel's 200 a run is past three times that.
        self.assertEqual(card["value"], "55")
        self.assertEqual(card["unit"], "min / run")
        self.assertEqual(card["sub"], "8 runs on 3 lines · last 30 days")
        self.assertEqual(card["tag"], {"label": "worst Sidel", "tone": "bad"})
        table = card["viz"]
        self.assertEqual(table["kind"], "table")
        self.assertEqual(table["columns"], ["Line", "Runs", "Breakdown / run"])
        self.assertEqual(table["rows"], [["Sidel", "2", "200 min"], ["Line A", "4", "10 min"], ["Unnamed line", "2", "0 min"]])
        self.assertEqual(table["aligns"], ["left", "right", "right"])
        self.assertEqual([cell["align"] for cell in table["body"][0]], ["left", "right", "right"])

    def test_production_line_downtime_is_neutral_when_no_line_stands_out(self):
        fake = factory(board_row(), [placement("production_line_downtime", 0, 0)])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != DOWNTIME_NEEDLE]
        fake.add(DOWNTIME_NEEDLE, [{"line_name": "A", "runs": 2, "breakdown": 20}, {"line_name": "B", "runs": 2, "breakdown": 10}])

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["value"], "8")
        self.assertEqual(card["tag"], {"label": "worst A", "tone": "neut"})

    def test_production_line_downtime_with_no_runs(self):
        fake = factory(board_row(), [placement("production_line_downtime", 0, 0, options={"window_days": "7"})])
        fake.answers = [(needle, rows) for needle, rows in fake.answers if needle != DOWNTIME_NEEDLE]

        card = build(fake)["cards"][0]["payload"]
        self.assertEqual(card["missing"], "No run was recorded in the last 7 days.")
        self.assertEqual(card["sub"], "Breakdown minutes per run")

    def test_the_production_read_hides_discarded_drafts_like_the_factorys_manager(self):
        self.assertIn("r.is_deleted = FALSE", DOWNTIME_SQL)
        self.assertIn("LEFT JOIN production_execution_productionline", DOWNTIME_SQL)

    def test_the_gate_reads_use_the_naive_local_pairs_and_no_company(self):
        self.assertIn("(a.gate_out_date + a.out_time) - (a.gate_in_date + a.in_time)", TURNAROUND_SQL)
        self.assertNotIn("departed_at", TURNAROUND_SQL)
        for sql in (TURNAROUND_SQL, ON_SITE_SQL):
            self.assertNotIn("company", sql)
        self.assertIn("d.is_active = TRUE", STAGE_LEGS_SQL)


class AbsenceTests(SimpleTestCase):

    def test_a_card_that_raises_costs_its_own_tile_and_not_the_board(self):
        def explode(context):
            raise RuntimeError("the query is wrong")

        spec = CardSpec(key="test_exploding", title="Exploding card", summary="Raises, on purpose.", category="Test", build=explode, needs_factory=False)
        with catalogue.temporarily(spec):
            payload = build(factory(board_row(), [
                placement("test_exploding", 0, 0),
                placement("gate_trucks_on_site", 1, 0),
            ]))

        cards = cards_by_key(payload)
        self.assertIsNone(cards["test_exploding"]["payload"])
        self.assertEqual(cards["gate_trucks_on_site"]["payload"]["value"], "3")
        self.assertEqual(payload["meta"]["degraded"], ["test_exploding"])
        self.assertEqual(payload["meta"]["reasons"]["test_exploding"], "RuntimeError: the query is wrong")
        self.assertEqual(payload["meta"]["withheld"], [])

    def test_a_card_returning_a_malformed_payload_is_degraded(self):
        spec = CardSpec(key="test_malformed", title="Malformed", summary="", category="Test", build=lambda context: viz.figure(value=""), needs_factory=False)
        with catalogue.temporarily(spec):
            payload = build(factory(board_row(), [placement("test_malformed", 0, 0)]))

        self.assertIsNone(payload["cards"][0]["payload"])
        self.assertIn("either a value or a reason", payload["meta"]["reasons"]["test_malformed"])

    def test_sap_down_degrades_only_the_cards_that_read_sap(self):
        hana = CountingHanaDown()
        with catalogue.temporarily(sap_spec("test_sap_a")), catalogue.temporarily(sap_spec("test_sap_b")):
            payload = build(
                factory(board_row(), [
                    placement("test_sap_a", 0, 0),
                    placement("gate_trucks_on_site", 1, 0),
                    placement("test_sap_b", 2, 0),
                    placement("dispatch_stage_clock", 0, 1),
                ]),
                hana=hana,
            )

        cards = cards_by_key(payload)
        self.assertIsNone(cards["test_sap_a"]["payload"])
        self.assertIsNone(cards["test_sap_b"]["payload"])
        self.assertEqual(cards["gate_trucks_on_site"]["payload"]["value"], "3")
        self.assertEqual(cards["dispatch_stage_clock"]["payload"]["value"], "2.25")
        self.assertEqual(payload["meta"]["degraded"], ["test_sap_a", "test_sap_b"])
        self.assertEqual(payload["meta"]["reasons"]["test_sap_b"], "SAP HANA could not be reached.")
        # The latch: one outage costs one attempt, not one per SAP card.
        self.assertEqual(hana.asked, 1)
        self.assertTrue(any("SAP did not answer" in warning for warning in payload["meta"]["warnings"]))

    def test_a_sap_card_reads_through_the_companys_schema(self):
        hana = FakeHana(company_code=COMPANY, schema="JIVO_OIL_LIVE").add("OINM", [{"n": 42}])
        with catalogue.temporarily(sap_spec()):
            payload = build(factory(board_row(), [placement("test_sap_rows", 0, 0)]), hana=hana)

        self.assertEqual(payload["cards"][0]["payload"]["value"], "42")
        self.assertIn('"JIVO_OIL_LIVE"."OINM"', hana.calls[0][0])
        self.assertEqual(payload["meta"]["degraded"], [])

    def test_a_company_without_a_sap_schema_degrades_its_sap_cards(self):
        with catalogue.temporarily(sap_spec()):
            service = BuiltBoardService(
                COMPANY,
                slug="night-shift",
                postgres=factory(board_row(), [placement("test_sap_rows", 0, 0), placement("section_label", 1, 0)]),
                hana_for=hana_factory(FakeHana(company_code="JIVO_MART")),
                today=TODAY,
            )
            payload = service.build()

        self.assertIsNone(payload["cards"][0]["payload"])
        self.assertIn("No SAP schema", payload["meta"]["reasons"]["test_sap_rows"])
        self.assertEqual(payload["cards"][1]["payload"]["value"], " ")

    def test_an_unknown_slug_is_a_board_of_nothing_with_a_warning(self):
        payload = build(factory(None, []), slug="nope")

        self.assertIsNone(payload["board"])
        self.assertEqual(payload["cards"], [])
        self.assertEqual(payload["meta"]["degraded"], [])
        self.assertEqual(payload["meta"]["retired"], [])
        self.assertEqual(payload["meta"]["slug"], "nope")
        self.assertTrue(any('"nope"' in warning for warning in payload["meta"]["warnings"]))

    def test_a_private_board_is_never_composed(self):
        # The fake answers whatever it is asked; the statement itself asks for PUBLISHED.
        fake = factory(board_row(visibility="PRIVATE"), [])
        build(fake)
        self.assertIn("b.visibility = %s", fake.calls[0][0])
        self.assertEqual(fake.calls[0][1][2], "PUBLISHED")

    def test_a_dead_factory_database_degrades_the_board_with_its_reason(self):
        down = Unavailable(FactoryUnavailable("The factory database could not be reached: refused"))
        payload = formatting.jsonable(
            BuiltBoardService(COMPANY, slug="night-shift", postgres=down, hana_for=hana_factory(HanaDown())).build()
        )

        self.assertIsNone(payload["board"])
        self.assertEqual(payload["cards"], [])
        self.assertEqual(payload["meta"]["degraded"], ["board"])
        self.assertIn("could not be reached", payload["meta"]["reasons"]["board"])
        self.assertTrue(any("factory database" in warning for warning in payload["meta"]["warnings"]))

    def test_a_rejected_placements_read_keeps_the_board_row(self):
        fake = RaisingPostgres(PLACEMENTS_NEEDLE, FactoryQueryError("permission denied for table board_builder_boardplacement"))
        fake.add(BOARD_NEEDLE, [board_row()])

        payload = build(fake)

        self.assertEqual(payload["board"]["slug"], "night-shift")
        self.assertEqual(payload["cards"], [])
        self.assertEqual(payload["meta"]["degraded"], ["placements"])
        self.assertIn("permission denied", payload["meta"]["reasons"]["placements"])

    def test_a_factory_outage_mid_board_latches_the_remaining_factory_cards(self):
        fake = RaisingPostgres(DISPATCH_NEEDLE, FactoryUnavailable("The factory database stopped answering: gone"))
        fake.add(BOARD_NEEDLE, [board_row()])
        fake.add(PLACEMENTS_NEEDLE, [
            placement("dispatch_stage_clock", 0, 0),
            placement("gate_trucks_on_site", 2, 0),
            placement("section_label", 3, 0, title="Gate"),
        ])
        fake.add(ON_SITE_NEEDLE, ON_SITE_ROWS)

        payload = build(fake)

        cards = cards_by_key(payload)
        self.assertIsNone(cards["dispatch_stage_clock"]["payload"])
        self.assertIsNone(cards["gate_trucks_on_site"]["payload"])
        # Furniture reads nothing, so it still draws.
        self.assertEqual(cards["section_label"]["payload"]["value"], " ")
        self.assertEqual(payload["meta"]["degraded"], ["dispatch_stage_clock", "gate_trucks_on_site"])
        self.assertEqual(len([call for call in fake.calls if ON_SITE_NEEDLE in call[0]]), 0)


# ---------------------------------------------------------------------------


class TemplateTests(SimpleTestCase):

    def full_payload(self, **board_overrides) -> Dict[str, Any]:
        payload = build(factory(board_row(**board_overrides), [
            placement("section_label", 0, 0, title="Gate"),
            placement("dispatch_stage_clock", 1, 0, accent="dispatch"),
            placement("gate_truck_turnaround", 0, 1),
            placement("gate_trucks_on_site", 2, 1),
            placement("production_line_downtime", 2, 2),   # hangs off the bottom: misplaced
        ]))
        payload["meta"]["company"] = COMPANY
        return payload

    def render(self, payload: Dict[str, Any]) -> str:
        return render_to_string("boards/fragments/built.html", {"board": payload, "meta": payload["meta"], "slide": registry.BUILT})

    def test_the_grid_is_sized_from_the_board_and_each_card_placed_from_its_origin(self):
        html = self.render(self.full_payload())

        self.assertIn('class="ops-board cbx-view cbx-s-light cbx-d-normal cbx-wall"', html)
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr)); grid-template-rows: repeat(3, minmax(0, 1fr));", html)
        # 1-based in CSS, spanning the card's own footprint.
        self.assertIn('data-card="section_label"', html)
        self.assertIn("grid-column: 1 / span 1; grid-row: 1 / span 1;", html)
        self.assertIn("grid-column: 2 / span 2; grid-row: 1 / span 1;", html)   # dispatch, 2x1 at (1,0)
        self.assertIn("grid-column: 1 / span 2; grid-row: 2 / span 1;", html)   # turnaround, 2x1 at (0,1)
        self.assertIn("grid-column: 3 / span 1; grid-row: 2 / span 1;", html)   # on site at (2,1)
        self.assertNotIn('data-card="production_line_downtime"', html)
        self.assertIn("no longer fit", html)   # said once, in the meta strip

    def test_the_heading_the_pill_and_the_clock(self):
        html = self.render(self.full_payload())

        self.assertIn('class="ops-pill-live"', html)
        self.assertIn('class="ops-clock" data-clock', html)
        self.assertIn('class="ops-title cbx-view-head"', html)
        self.assertIn("<b>Night shift</b>", html)
        self.assertIn("<span>The lines, after six</span>", html)

    def test_show_heading_off_keeps_the_pill_and_clock_but_not_the_name(self):
        html = self.render(self.full_payload(show_heading=False))

        self.assertNotIn("Night shift", html)
        self.assertNotIn("cbx-view-head", html)
        self.assertIn("<b>Built board</b>", html)
        self.assertIn('data-clock', html)

    def test_surface_density_and_accent_are_on_the_board(self):
        html = self.render(self.full_payload(surface="dark", density="compact", accent="production"))

        self.assertIn("cbx-s-dark", html)
        self.assertIn("cbx-d-compact", html)
        self.assertIn('data-surface="dark" data-density="compact"', html)
        self.assertIn('data-accent="production"', html)
        # A card without its own hue wears the board's; one with its own keeps it.
        self.assertIn('class="cbx-cell ops-b-production" data-card="gate_trucks_on_site"', html)
        self.assertIn('class="cbx-cell ops-b-dispatch" data-card="dispatch_stage_clock"', html)

    def test_a_page_board_gets_rows_that_can_grow(self):
        html = self.render(self.full_payload(mode="PAGE"))
        self.assertNotIn("cbx-wall", html)
        self.assertIn("grid-template-rows: repeat(3, minmax(calc(14 * var(--u)), auto));", html)

    def test_the_figures_the_tags_and_the_shapes(self):
        html = self.render(self.full_payload())

        self.assertIn("<b>2.25</b><u>h average</u>", html)
        self.assertIn('<span class="ops-tag ops-t-warn">67% loading</span>', html)
        self.assertIn('<div class="ops-sub">2 dockings · last 30 days</div>', html)
        self.assertIn('<i class="ops-f-main" style="width: 100.0%;"></i>', html)
        self.assertIn('<i class="ops-f-light" style="width: 50.0%;"></i>', html)
        self.assertIn("<strong>1.50</strong>", html)
        self.assertIn('<span class="ops-k-mute">Average hours per leg, one scale</span>', html)

        self.assertIn("<b>4.00</b><u>h median</u>", html)
        self.assertIn('<div class="ops-now">', html)
        self.assertIn('<span class="ops-now">Tue</span>', html)
        self.assertIn('<i style="height: 100.0%; animation-delay: 0.20s;"></i>', html)   # Thursday
        self.assertIn('<i style="height: 0.0%; animation-delay: 0.14s;"></i>', html)     # Wednesday, nothing
        self.assertIn('<em class="ops-bval">10.0</em>', html)
        self.assertIn('<p class="cbx-note">Median hours on site by weekday. Today is picked out.</p>', html)

        self.assertIn("<b>3</b><u>on site</u>", html)
        self.assertIn('<span class="ops-tag ops-t-bad">1 stale</span>', html)
        self.assertIn('<div class="cbx-split">', html)
        self.assertIn("<div><b>2</b><em>Inside</em></div>", html)

        self.assertIn('<b class="ops-nm">Gate</b>', html)
        self.assertIn('data-placement="100"', html)

    def test_a_table_and_a_matrix(self):
        html = render_card(viz.figure(value="55", unit="min / run", viz=viz.table(
            columns=["Line", "Runs"], rows=[["Sidel", "2"]], aligns=["left", "right"]
        )))
        self.assertIn('<table class="cbx-table">', html)
        self.assertIn('<th style="text-align: right;">Runs</th>', html)
        self.assertIn('<td title="Sidel" style="text-align: left;">Sidel</td>', html)
        self.assertIn('<td title="2" style="text-align: right;">2</td>', html)

        html = render_card(viz.figure(value="7", viz=viz.matrix(
            columns=["GRPO", "AP"], rows=[("0–14", [3, None])], caption="Open documents by age"
        )))
        self.assertIn('<table class="cbx-table cbx-matrix">', html)
        self.assertIn('<th scope="row">0–14</th>', html)
        self.assertIn('<span class="cbx-nil">—</span>', html)
        self.assertIn('<p class="cbx-caption">Open documents by age</p>', html)

    def test_a_meter(self):
        html = render_card(viz.figure(value="40", viz=viz.meter([("Filled", 30, "main"), ("Empty", 10, "mute"), ("", 0, "light")])))
        self.assertIn('<i class="ops-f-main" style="width: 75.0%; min-width: var(--meter-h);"></i>', html)
        self.assertIn('<i class="ops-f-mute" style="width: 25.0%; min-width: var(--meter-h); animation-delay: 0.1s;"></i>', html)
        self.assertIn('<i class="ops-f-light" style="width: 0.0%; animation-delay: 0.2s;"></i>', html)
        self.assertIn('<span class="ops-k-main">Filled<b>30.0</b></span>', html)
        self.assertNotIn('<span class="ops-k-light">', html)

    def test_a_missing_figure_is_a_rule_with_its_reason_and_never_a_condition_colour(self):
        payload = viz.missing("No truck completed a trip in the last 30 days.", sub="Gate to gate")
        payload["tag"] = viz.tag("late", "bad")
        html = render_card(payload)

        self.assertIn('<b class="ops-nil">—</b>', html)
        self.assertIn('<p class="ops-note">No truck completed a trip in the last 30 days.</p>', html)
        self.assertIn('<div class="ops-sub">Gate to gate</div>', html)
        self.assertIn("ops-t-nil", html)
        self.assertNotIn("ops-t-bad", html)

    def test_a_card_that_was_not_read_is_an_unread_tile_not_a_zero(self):
        html = render_card(None)

        self.assertIn('<div class="ops-unread cbx-unread"><b>Not read</b>SAP HANA could not be reached.</div>', html)
        self.assertIn('<b class="ops-nm">Trucks on site</b>', html)
        self.assertNotIn("<b>0</b>", html)
        self.assertIn("Gate trucks on site: not read", html)   # the meta strip

    def test_a_board_that_could_not_be_read(self):
        down = Unavailable(FactoryUnavailable("The factory database could not be reached: refused"))
        payload = formatting.jsonable(
            BuiltBoardService(COMPANY, slug="night-shift", postgres=down, hana_for=hana_factory(HanaDown())).build()
        )
        html = self.render(payload)

        self.assertIn('class="ops-unread cbx-unread cbx-no-board"', html)
        self.assertIn("<b>Built board could not be read</b>The factory database could not be reached: refused", html)
        self.assertNotIn("cbx-view-grid", html)
        self.assertIn("Board: not read", html)
        self.assertIn('data-clock', html)

    def test_an_unknown_slug(self):
        payload = build(factory(None, []), slug="nope")
        html = self.render(payload)

        self.assertIn("No published board is slugged “nope”", html)
        self.assertIn("taken off the rotation", html)
        self.assertNotIn("cbx-view-grid", html)
        self.assertNotIn("bcx-meta__chip--degraded", html)

    def test_an_empty_board_says_so(self):
        payload = build(factory(board_row(), []))
        payload["meta"]["company"] = COMPANY
        html = self.render(payload)

        self.assertIn("cbx-view-grid", html)
        self.assertIn("There is nothing on this board yet.", html)

    def test_the_fragment_ends_with_the_meta_strip_inside_the_board(self):
        html = self.render(self.full_payload())
        board_end = html.rstrip().rfind("</div>")
        self.assertLess(html.find("bcx-meta"), board_end)

    def test_the_slides_assets_exist(self):
        for path in (*registry.BUILT.css, *registry.BUILT.js):
            self.assertIsNotNone(finders.find(path), path)
        # Every hue a card may wear is defined by one of the two sheets the
        # page loads: the factory's three in ops-board.css, the rest here.
        with open(finders.find("boards/built.css"), encoding="utf-8") as handle:
            own = handle.read()
        with open(finders.find("boards/ops-board.css"), encoding="utf-8") as handle:
            shared = handle.read()
        for accent in CARD_ACCENTS:
            self.assertIn(f".ops-b-{accent} {{", own + shared, accent)
        for accent in ("purchase", "store", "production", "shifting"):
            self.assertIn(f".ops-b-{accent} {{", own, accent)
        with open(finders.find("boards/built.js"), encoding="utf-8") as handle:
            self.assertIn("boards:mounted", handle.read())
