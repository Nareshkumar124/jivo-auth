"""
Tests for the carousel shell. Nothing here reaches the factory database or
SAP: the sources are switched off with override_settings and every service
under test takes fakes.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, Permission
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.html import escape

from users.models import User

from . import formatting, registry
from .compose import build_board, render_fragment
from .sections import SectionBuilder
from .sources import (
    FactoryNotConfigured,
    FactoryUnavailable,
    Hana,
    SAPConnectionError,
    SAPDataError,
    SAPNotConfigured,
    Unavailable,
    connect,
)
from .templatetags.boards import day_label, dec, indian, money, qty, section_label, share, whole
from .testing import FakeHana, FakePostgres, HanaDown, hana_factory


PASSWORD = "Str0ng!Pass"

FACTORY_OFF = {
    "NAME": "",
    "USER": "",
    "PASSWORD": "",
    "HOST": "",
    "PORT": 5432,
    "CONNECT_TIMEOUT": 1,
    "STATEMENT_TIMEOUT": 1,
}

HANA_OFF = {
    "HOST": "",
    "PORT": 30015,
    "USER": "",
    "PASSWORD": "",
    "SCHEMAS": {},
    "CONNECT_TIMEOUT": 1,
    "COMMUNICATION_TIMEOUT": 1,
}


def permission():
    return Permission.objects.get(
        content_type__app_label="boards", codename="view_dashboards"
    )


@override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF, BOARDS_CACHE_SECONDS=0)
class BoardsTestCase(TestCase):

    def setUp(self):
        cache.clear()
        self.superuser = User.objects.create_superuser(email="root@jivo.in", password=PASSWORD)
        # Staff status gives nothing here; the permission does.
        self.staff = User.objects.create_user(
            email="staff@jivo.in", password=PASSWORD, is_staff=True, is_verified=True
        )
        self.plain = User.objects.create_user(email="amit@jivo.in", password=PASSWORD, is_verified=True)
        # A wall screen's account: not staff, only the dashboards permission.
        self.display = User.objects.create_user(email="wall@jivo.in", password=PASSWORD, is_verified=True)
        self.display.user_permissions.add(permission())


class AccessTests(BoardsTestCase):

    def pages(self):
        return [
            reverse("boards:index"),
            reverse("boards:carousel"),
            reverse("boards:board", args=["accounts"]),
            reverse("boards:screen", args=["accounts"]),
            reverse("boards:built", args=["night-shift"]),
            reverse("boards:built-screen", args=["night-shift"]),
        ]

    def reads(self):
        return [
            reverse("boards:fragment", args=["accounts"]),
            reverse("boards:data", args=["accounts"]),
            reverse("boards:built-fragment", args=["night-shift"]),
            reverse("boards:built-data", args=["night-shift"]),
        ]

    def test_the_dashboards_live_under_slash_dashboards(self):
        self.assertEqual(reverse("boards:index"), "/dashboards/")
        self.assertEqual(reverse("boards:carousel"), "/dashboards/carousel/")
        self.assertEqual(reverse("boards:board", args=["plant"]), "/dashboards/plant/")

    def test_visitors_are_sent_to_the_ticket_systems_sign_in(self):
        for url in self.pages():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("support:login")), response["Location"])

    def test_the_reads_answer_403_so_the_page_reloads_into_the_sign_in(self):
        for url in self.reads():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_people_without_the_permission_are_refused_even_staff(self):
        for who in (self.plain, self.staff):
            self.client.force_login(who)

            for url in self.pages() + self.reads():
                with self.subTest(who=who.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_permission_opens_every_page_without_staff_status(self):
        self.client.force_login(self.display)

        for url in self.pages() + self.reads():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_superusers_see_them_without_the_permission(self):
        self.client.force_login(self.superuser)

        self.assertEqual(self.client.get(reverse("boards:index")).status_code, 200)

    def test_the_dashboard_viewer_staff_role_grants_it(self):
        group = Group.objects.get(name="Dashboard viewer")
        self.assertEqual(list(group.permissions.values_list("codename", flat=True)), ["view_dashboards"])

        self.plain.groups.add(group)
        self.client.force_login(User.objects.get(pk=self.plain.pk))

        self.assertEqual(self.client.get(reverse("boards:index")).status_code, 200)

    def test_unknown_board_is_404(self):
        self.client.force_login(self.display)

        self.assertEqual(self.client.get("/dashboards/nope/").status_code, 404)
        self.assertEqual(self.client.get("/dashboards/nope/fragment/").status_code, 404)

    def test_nothing_is_left_in_the_django_admin(self):
        self.client.force_login(self.superuser)

        page = self.client.get(reverse("admin:index"))

        self.assertNotContains(page, "/dashboards/")
        self.assertEqual(self.client.get("/boards/carousel/").status_code, 404)


class DashboardSectionTests(BoardsTestCase):

    def test_the_sidebar_has_a_dashboard_section_for_those_who_may_see_it(self):
        self.client.force_login(self.display)
        page = self.client.get("/")
        titles = [section["title"] for section in page.context["nav_sections"]]
        self.assertIn("Dashboard", titles)
        items = next(section["items"] for section in page.context["nav_sections"] if section["title"] == "Dashboard")
        self.assertEqual(
            [item["label"] for item in items],
            ["All dashboards", "Admin Control", "Plant Control", "Logistics Control", "Accounts", "Wall carousel"],
        )

        self.client.force_login(self.plain)
        titles = [section["title"] for section in self.client.get("/").context["nav_sections"]]
        self.assertNotIn("Dashboard", titles)

    def test_overview_lists_every_board(self):
        self.client.force_login(self.display)

        page = self.client.get(reverse("boards:index"))

        self.assertTemplateUsed(page, "support/base.html")
        for slide in registry.SLIDES:
            self.assertContains(page, reverse("boards:board", args=[slide.key]))
            self.assertContains(page, escape(slide.label))
        self.assertContains(page, reverse("boards:carousel"))
        self.assertContains(page, "No built board is on the rotation")

    def test_a_board_page_frames_its_screen(self):
        self.client.force_login(self.display)

        page = self.client.get(reverse("boards:board", args=["plant"]))

        self.assertTemplateUsed(page, "support/base.html")
        self.assertContains(page, "Plant control board")
        self.assertContains(page, f'src="{reverse("boards:screen", args=["plant"])}?embed=1"')
        self.assertContains(page, "data-fullscreen-target")

    def test_the_screen_may_be_framed_by_this_site_only(self):
        self.client.force_login(self.display)

        screen = self.client.get(reverse("boards:screen", args=["plant"]) + "?embed=1")
        self.assertEqual(screen["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(screen, "bcx-embedded")

        page = self.client.get(reverse("boards:board", args=["plant"]))
        self.assertEqual(page["X-Frame-Options"], "DENY")

        carousel = self.client.get(reverse("boards:carousel"))
        self.assertEqual(carousel["X-Frame-Options"], "DENY")

        # Template comments never reach the screen (a {# … #} only works on one line).
        for response in (screen, page, carousel):
            self.assertNotContains(response, "{#")
            self.assertNotContains(response, "#}")
        self.assertContains(carousel, f'href="{reverse("boards:index")}"')
        self.assertNotContains(carousel, "bcx-embedded")


class ShellTests(BoardsTestCase):

    def setUp(self):
        super().setUp()
        self.client.force_login(self.display)

    def test_fragment_is_html_and_never_cached(self):
        response = self.client.get(reverse("boards:fragment", args=["admin"]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, "ops-board")

    def test_data_is_the_same_board_as_json(self):
        response = self.client.get(reverse("boards:data", args=["admin"]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["meta"]["slide"], "admin")
        self.assertEqual(payload["meta"]["label"], "Admin Control")
        self.assertIn("generated_at", payload["meta"])
        self.assertIn("refresh_seconds", payload["meta"])

    def test_a_single_board_screen_marks_itself_single(self):
        response = self.client.get(reverse("boards:screen", args=["plant"]))

        self.assertContains(response, "bcx-single")
        self.assertContains(response, "Plant control board")

    def test_built_boards_join_the_rotation_after_the_fixed_ones(self):
        fake = FakePostgres()
        boards = [{"slug": "night-shift", "name": "Night shift"}]

        with mock.patch("boards.views.connect") as connect_mock, mock.patch(
            "boards.services.built.list_carousel_boards", return_value=boards
        ):
            connect_mock.return_value.__enter__.return_value = fake
            response = self.client.get(reverse("boards:carousel"))
            overview = self.client.get(reverse("boards:index"))

        self.assertContains(response, "Night shift")
        self.assertContains(response, reverse("boards:built-fragment", args=["night-shift"]))
        content = response.content.decode()
        self.assertLess(content.index("Accounts"), content.index("Night shift"))
        self.assertContains(overview, reverse("boards:built", args=["night-shift"]))

    def test_built_board_list_failure_leaves_the_fixed_rotation(self):
        with mock.patch("boards.services.built.list_carousel_boards", side_effect=RuntimeError("boom")):
            response = self.client.get(reverse("boards:carousel"))

        self.assertEqual(response.status_code, 200)

    def test_built_board_fragment_renders(self):
        response = self.client.get(reverse("boards:built-fragment", args=["night-shift"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(reverse("boards:built-data", args=["night-shift"])).json()["meta"]["slug"],
            "night-shift",
        )

    @override_settings(BOARDS_CACHE_SECONDS=60)
    def test_a_composed_board_is_reused_from_the_cache(self):
        with mock.patch("boards.compose.build_board", wraps=build_board) as build:
            self.client.get(reverse("boards:data", args=["admin"]))
            self.client.get(reverse("boards:data", args=["admin"]))

        self.assertEqual(build.call_count, 1)


class RegistryTests(TestCase):

    def test_every_slide_has_a_service_a_template_and_its_assets(self):
        for slide in (*registry.SLIDES, registry.BUILT):
            with self.subTest(slide=slide.key):
                self.assertTrue(callable(slide.service_class()))
                get_template(slide.template)
                for path in (*slide.css, *slide.js):
                    self.assertIsNotNone(finders.find(path), path)

    def test_shared_assets_exist(self):
        for path in ("boards/ops-board.css", "boards/carousel.css", "boards/boards.css", "boards/carousel.js"):
            self.assertIsNotNone(finders.find(path), path)

    def test_keys_are_unique(self):
        keys = [slide.key for slide in registry.SLIDES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("built", keys)


class SectionTests(TestCase):

    def builder(self):
        builder = SectionBuilder()
        builder._init_sections()
        return builder

    def test_a_section_that_raises_is_degraded_with_its_reason(self):
        builder = self.builder()

        def boom():
            raise ValueError("bad column")

        self.assertIsNone(builder.section("tile", boom, needs_sap=False))
        meta = builder.section_meta()
        self.assertEqual(meta["degraded"], ["tile"])
        self.assertEqual(meta["reasons"]["tile"], "ValueError: bad column")
        self.assertEqual(meta["warnings"], [])

    def test_a_sap_outage_latches_for_the_rest_of_the_build(self):
        builder = self.builder()
        calls = []

        def down():
            calls.append("sap")
            raise SAPConnectionError("SAP HANA could not be reached.")

        def never():
            calls.append("never")

        builder.section("first", down)
        builder.section("second", never)
        builder.section("postgres-only", lambda: 1, needs_sap=False)

        self.assertEqual(calls, ["sap"])
        meta = builder.section_meta()
        self.assertEqual(meta["degraded"], ["first", "second"])
        self.assertEqual(meta["reasons"]["second"], "SAP HANA could not be reached.")
        self.assertTrue(any("SAP did not answer" in warning for warning in meta["warnings"]))

    def test_a_missing_setting_is_not_reported_as_an_outage(self):
        builder = self.builder()

        def no_sap():
            raise SAPNotConfigured("SAP HANA is not configured on this service.")

        def no_database():
            raise FactoryNotConfigured("The factory database is not configured on this service.")

        builder.section("sap", no_sap)
        builder.section("db", no_database, needs_sap=False)
        warnings = builder.section_meta()["warnings"]
        self.assertEqual(
            warnings,
            [
                "SAP is not configured on this service. The tiles that read it show nothing.",
                "The factory database is not configured on this service. The tiles that read it show nothing.",
            ],
        )

    def test_a_sap_data_error_costs_one_section_only(self):
        builder = self.builder()

        def rejected():
            raise SAPDataError("SAP rejected this query: bad column")

        builder.section("first", rejected)
        self.assertEqual(builder.section("second", lambda: 2), 2)
        self.assertEqual(builder.section_meta()["degraded"], ["first"])

    def test_a_factory_outage_latches_too(self):
        builder = self.builder()

        def down():
            raise FactoryUnavailable("The factory database could not be reached: refused")

        builder.section("first", down, needs_sap=False)
        builder.section("second", lambda: 2, needs_sap=False)
        self.assertEqual(builder.section("sap-only", lambda: 3, needs_factory=False), 3)

        meta = builder.section_meta()
        self.assertEqual(meta["degraded"], ["first", "second"])
        self.assertTrue(any("factory database" in warning for warning in meta["warnings"]))

    def test_warnings_are_not_repeated(self):
        builder = self.builder()
        builder.warn("x")
        builder.warn("x")
        self.assertEqual(builder.section_meta()["warnings"], ["x"])


class ReadingService(SectionBuilder):
    """A service that reads both sources, for the compose tests."""

    def __init__(self, company_code, *, postgres, hana_for, today=None):
        self.postgres = postgres
        self.hana = hana_for(company_code)
        self._init_sections()

    def build(self):
        return {
            "cash": self.section("cash", lambda: self.postgres.value("select sum(amount) from t"), needs_sap=False),
            "stock": self.section("stock", lambda: self.hana.rows(f"select * from {self.hana.table('OITW')}"), needs_factory=False),
            "meta": self.section_meta(),
        }


READING = registry.Slide(
    key="reading",
    label="Reading",
    title="Reading",
    service="boards.tests.ReadingService",
    template="boards/fragments/_stub.html",
)


@override_settings(FACTORY_DB=FACTORY_OFF, FACTORY_HANA=HANA_OFF)
class ComposeTests(TestCase):

    def test_unconfigured_sources_degrade_with_a_reason_that_names_the_setting(self):
        payload = build_board(READING)

        self.assertIsNone(payload["cash"])
        self.assertIsNone(payload["stock"])
        self.assertEqual(payload["meta"]["degraded"], ["cash", "stock"])
        self.assertIn("FACTORY_DB_NAME", payload["meta"]["reasons"]["cash"])
        self.assertIn("FACTORY_HANA_HOST", payload["meta"]["reasons"]["stock"])
        self.assertEqual(payload["meta"]["slide"], "reading")

    def test_fakes_feed_the_sections(self):
        fake = FakePostgres().add("sum(amount)", [{"sum": Decimal("12.50")}])
        hana = FakeHana().add("OITW", [{"WhsCode": "BH-BT", "OnHand": Decimal("3")}])

        with mock.patch("boards.compose.connect") as connect_mock, mock.patch(
            "boards.compose.hana_for", hana_factory(hana)
        ):
            connect_mock.return_value.__enter__.return_value = fake
            payload = build_board(READING)

        self.assertEqual(payload["cash"], 12.5)
        self.assertEqual(payload["stock"], [{"WhsCode": "BH-BT", "OnHand": 3.0}])
        self.assertEqual(payload["meta"]["degraded"], [])

    def test_a_company_without_a_schema_degrades_its_sap_tiles(self):
        with mock.patch("boards.compose.hana_for", hana_factory(FakeHana(company_code="JIVO_MART"))):
            payload = build_board(READING, company_code="JIVO_OIL")

        self.assertIn("No SAP schema", payload["meta"]["reasons"]["stock"])

    def test_payloads_are_plain_json(self):
        payload = build_board(READING)
        self.assertIsInstance(payload["meta"]["generated_at"], str)

    def test_the_fragment_lists_what_was_not_read(self):
        html = render_fragment(READING, build_board(READING))

        self.assertIn("bcx-meta__chip--degraded", html)
        self.assertIn("Cash: not read", html)

    def test_many_unread_sections_become_one_chip(self):
        meta = {"degraded": ["a_one", "b", "c", "d", "e_five"], "reasons": {"a_one": "SAP is down."}, "warnings": []}

        html = get_template("boards/fragments/_meta.html").render({"meta": meta})

        self.assertEqual(html.count("bcx-meta__chip--degraded"), 1)
        self.assertIn("5 sections not read", html)
        self.assertIn("A one: SAP is down.", html)


class SourceTests(TestCase):

    @override_settings(FACTORY_DB=FACTORY_OFF)
    def test_connect_refuses_when_unconfigured(self):
        with self.assertRaises(FactoryUnavailable) as raised:
            with connect():
                pass
        self.assertIn("FACTORY_DB_NAME", str(raised.exception))

    @override_settings(FACTORY_DB={**FACTORY_OFF, "NAME": "x", "USER": "y", "HOST": "203.0.113.1", "PORT": 1})
    def test_an_unreachable_database_is_unavailable(self):
        with mock.patch("boards.sources.postgres.psycopg.connect", side_effect=OSError("refused")):
            with self.assertRaises(Exception):
                with connect():
                    pass

    @override_settings(FACTORY_HANA=HANA_OFF)
    def test_hana_refuses_when_unconfigured(self):
        with self.assertRaises(SAPConnectionError) as raised:
            Hana("JIVO_OIL").rows("select 1 from dummy")
        self.assertIn("FACTORY_HANA_HOST", str(raised.exception))

    @override_settings(FACTORY_HANA={**HANA_OFF, "HOST": "h", "USER": "u", "SCHEMAS": {"JIVO_MART": "MART"}})
    def test_hana_refuses_a_company_without_a_schema(self):
        with self.assertRaises(SAPConnectionError):
            Hana("JIVO_OIL").table("OITW")
        self.assertEqual(Hana("JIVO_MART").table("OITW"), '"MART"."OITW"')

    def test_unavailable_stand_in_reraises(self):
        error = FactoryUnavailable("gone")
        stand_in = Unavailable(error)
        for call in (lambda: stand_in.rows("x"), lambda: stand_in.one("x"), lambda: stand_in.value("x")):
            with self.assertRaises(FactoryUnavailable):
                call()

    def test_hana_down_fake_raises(self):
        with self.assertRaises(SAPConnectionError):
            HanaDown().rows("x")


class FormattingTests(TestCase):

    def test_indian_grouping(self):
        self.assertEqual(indian(999), "999")
        self.assertEqual(indian(1234), "1,234")
        self.assertEqual(indian(1234567), "12,34,567")
        self.assertEqual(indian(123456789.5, 1), "12,34,56,789.5")
        self.assertEqual(indian(-1234567), "-12,34,567")
        # Rounded before the split: the carry reaches the whole part.
        self.assertEqual(indian(612.97, 1), "613.0")
        self.assertEqual(indian(2.96, 1), "3.0")
        self.assertEqual(indian(99999.996, 2), "1,00,000.00")
        # Halves round up, as in the factory's browser.
        self.assertEqual(indian(32.5), "33")
        self.assertEqual(indian(0.125, 2), "0.13")
        self.assertEqual(indian(-0.4), "0")
        self.assertEqual(dec(612.97), "613.0")

    def test_filters_degrade_to_a_rule(self):
        for value in (None, "x", float("nan")):
            self.assertEqual(whole(value), "—")
            self.assertEqual(qty(value), "—")
            self.assertEqual(money(value), "—")
            self.assertEqual(dec(value), "—")

    def test_quantities_and_money(self):
        self.assertEqual(qty(12345), "12,345")
        self.assertEqual(qty(1_50_000), "1.50 L")
        self.assertEqual(qty(2_50_00_000), "2.50 Cr")
        self.assertEqual(money(950), "₹950")
        self.assertEqual(money(12_500), "₹12.5k")
        self.assertEqual(money(3_40_000), "₹3.4 L")
        self.assertEqual(money(1_20_00_000), "₹1.20 Cr")
        self.assertEqual(dec(1234.567), "1,234.6")

    def test_day_labels_are_not_shifted_by_a_zone(self):
        self.assertEqual(day_label("2026-09-10"), "10 Sep")
        self.assertEqual(day_label("2026-09-10T18:30:00"), "10 Sep")
        self.assertEqual(day_label("nope"), "—")

    def test_section_labels_read_as_people_write_them(self):
        self.assertEqual(section_label("cash_issued"), "Cash issued")
        self.assertEqual(section_label("pending_ho"), "Pending HO")
        self.assertEqual(section_label("open_pos"), "Open POs")
        self.assertEqual(section_label("grpo-queue"), "GRPO queue")
        self.assertEqual(section_label("pf_stock"), "PF stock")
        self.assertEqual(section_label(None), "")

    def test_share_is_never_nan(self):
        self.assertEqual(share(5, 0), 0.0)
        self.assertEqual(share(None, 10), 0.0)
        self.assertEqual(share(5, 10), 50.0)
        self.assertEqual(share(50, 10), 100.0)

    def test_helpers(self):
        self.assertEqual(formatting.tons(1500), 1.5)
        self.assertEqual(formatting.lakhs(250000), "2.5 L")
        self.assertEqual(formatting.lakhs(12000000), "1.20 Cr")
        self.assertEqual(formatting.pct(1, 4), 25.0)
        self.assertIsNone(formatting.pct(1, 0))
        self.assertEqual(formatting.jsonable({"a": Decimal("1.5"), "b": [formatting.today()]})["a"], 1.5)
        self.assertEqual(str(formatting.as_date("2026-09-10 00:00:00")), "2026-09-10")
