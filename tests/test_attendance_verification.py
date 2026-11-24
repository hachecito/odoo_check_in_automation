import unittest
from unittest.mock import patch

import odoo_checkin
from odoo_checkin import create_attendance_context, wait_for_attendance_transition
from playwright.sync_api import sync_playwright


class LocationFallbackDialogTests(unittest.TestCase):
    def test_accepts_only_the_explicit_no_location_dialog(self):
        html = """
        <div role="dialog">
          <p>No se puede obtener una ubicación válida. ¿Deseas continuar con el registro de entrada/salida de todos modos?</p>
          <button id="continue" onclick="document.body.dataset.action='continue'">Continuar de todos modos</button>
          <button id="cancel" onclick="document.body.dataset.action='cancel'">Cancelar</button>
        </div>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            try:
                accepted = odoo_checkin.accept_location_fallback_dialog(page, timeout_ms=100)
                action = page.locator("body").get_attribute("data-action")
            finally:
                browser.close()

        self.assertTrue(accepted)
        self.assertEqual(action, "continue")

    def test_does_not_accept_an_unrelated_confirmation_dialog(self):
        html = """
        <div role="dialog">
          <p>¿Deseas eliminar este registro?</p>
          <button id="confirm">Confirmar</button>
          <button id="cancel">Cancelar</button>
        </div>
        """
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html)
            try:
                accepted = odoo_checkin.accept_location_fallback_dialog(page, timeout_ms=50)
                action = page.locator("body").get_attribute("data-action")
            finally:
                browser.close()

        self.assertFalse(accepted)
        self.assertIsNone(action)


class WaitForAttendanceTransitionTests(unittest.TestCase):
    def test_waits_for_odoo_to_reflect_a_delayed_checkout(self):
        open_record = {"check_in": "2026-11-20 08:00:00", "check_out": False}
        closed_record = {"check_in": "2026-11-20 08:00:00", "check_out": "2026-11-20 17:35:00"}
        responses = iter([open_record, open_record, closed_record])

        with patch("odoo_checkin.time.monotonic", side_effect=[0, 0, 1]), patch("odoo_checkin.time.sleep"):
            result = wait_for_attendance_transition(lambda: next(responses), was_open=True, timeout=10)

        self.assertEqual(result, closed_record)

    def test_returns_none_if_odoo_never_changes_the_open_state(self):
        open_record = {"check_in": "2026-11-20 08:00:00", "check_out": False}

        with patch("odoo_checkin.time.monotonic", side_effect=[0, 10]), patch("odoo_checkin.time.sleep"):
            result = wait_for_attendance_transition(lambda: open_record, was_open=True, timeout=10)

        self.assertIsNone(result)


class CreateAttendanceContextTests(unittest.TestCase):
    def test_creates_fresh_context_without_granting_geolocation_by_default(self):
        class FakeContext:
            granted = []

            def grant_permissions(self, permissions, origin=None):
                self.granted.append((permissions, origin))

        class FakeBrowser:
            def __init__(self):
                self.context = FakeContext()

            def new_context(self):
                return self.context

        browser = FakeBrowser()
        context = create_attendance_context(browser, "https://octupus.odoo.com/web/login")

        self.assertIs(context, browser.context)
        self.assertEqual(context.granted, [])


class ConfigureGeolocationTests(unittest.TestCase):
    def test_injects_live_location_and_grants_permission_only_to_odoo(self):
        class FakeContext:
            def __init__(self):
                self.location = None
                self.grants = []

            def set_geolocation(self, location):
                self.location = location

            def grant_permissions(self, permissions, origin=None):
                self.grants.append((permissions, origin))

        context = FakeContext()
        location = {"latitude": 12.3, "longitude": -45.6, "accuracy": 25000}

        configured = odoo_checkin.configure_attendance_geolocation(
            context, "https://octupus.odoo.com/web/login", location
        )

        self.assertTrue(configured)
        self.assertEqual(context.location, location)
        self.assertEqual(context.grants, [(["geolocation"], "https://octupus.odoo.com")])

    def test_keeps_location_unavailable_without_granting_browser_permission(self):
        class FakeContext:
            def __init__(self):
                self.location = None
                self.grants = []

            def set_geolocation(self, location):
                self.location = location

            def grant_permissions(self, permissions, origin=None):
                self.grants.append((permissions, origin))

        context = FakeContext()
        configured = odoo_checkin.configure_attendance_geolocation(
            context, "https://octupus.odoo.com/web/login", None
        )

        self.assertFalse(configured)
        self.assertIsNone(context.location)
        self.assertEqual(context.grants, [])


class QueryGeoClueLocationTests(unittest.TestCase):
    def test_parses_dynamic_geo_clue_result_without_logging_coordinates(self):
        completed = type("Completed", (), {
            "stdout": '{"latitude": 12.3, "longitude": -45.6, "accuracy": 25000}'
        })()
        with patch("odoo_checkin.subprocess.run", return_value=completed) as run:
            result = odoo_checkin.query_geoclue_location()

        self.assertEqual(result, {"latitude": 12.3, "longitude": -45.6, "accuracy": 25000})
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/python3")
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_returns_none_for_failed_or_invalid_geo_clue_result(self):
        with patch("odoo_checkin.subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(odoo_checkin.query_geoclue_location())
        completed = type("Completed", (), {"stdout": '{"latitude": 1000, "longitude": 0, "accuracy": 2}'})()
        with patch("odoo_checkin.subprocess.run", return_value=completed):
            self.assertIsNone(odoo_checkin.query_geoclue_location())


class VerifyPageGeolocationTests(unittest.TestCase):
    def test_reports_only_match_and_accuracy_from_browser_position(self):
        class FakePage:
            def evaluate(self, _expression, expected):
                self.expected = expected
                return {"ok": True, "matches_injected": True, "accuracy_m": 25000}

        page = FakePage()
        location = {"latitude": 12.3, "longitude": -45.6, "accuracy": 25000}
        result = odoo_checkin.verify_page_geolocation(page, location)

        self.assertEqual(page.expected, {"latitude": 12.3, "longitude": -45.6})
        self.assertEqual(result, {"ok": True, "matches_injected": True, "accuracy_m": 25000})

    def test_reports_unavailable_without_a_geo_clue_fix(self):
        class FakePage:
            def evaluate(self, *_args):
                raise AssertionError("must not request browser location without a GeoClue fix")

        self.assertEqual(
            odoo_checkin.verify_page_geolocation(FakePage(), None),
            {"ok": False, "reason": "geoclue_unavailable"},
        )


if __name__ == "__main__":
    unittest.main()
