"""Odoo 19 attendance check-in/check-out via Playwright, skipping weekends, leaves and holidays."""

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

EXIT_DONE, EXIT_ERROR, EXIT_SKIPPED = 0, 1, 10
ODOO_DT = "%Y-%m-%d %H:%M:%S"
BASE_DIR = Path(__file__).resolve().parent

GEOCLUE_QUERY = r'''
import json
import gi
gi.require_version("Geoclue", "2.0")
from gi.repository import Geoclue

client = Geoclue.Simple.new_sync("google-chrome", Geoclue.AccuracyLevel.EXACT, None)
location = client.get_location()
print(json.dumps({
    "latitude": location.get_property("latitude"),
    "longitude": location.get_property("longitude"),
    "accuracy": location.get_property("accuracy"),
}))
'''


def emit(status, reason, exit_code, **extra):
    """Print a JSON result line for Hermes and exit.
    :param status: done | skipped | error
    :param reason: short machine-readable reason
    :param exit_code: process exit code
    """
    print(json.dumps({"status": status, "reason": reason, **extra}, default=str, ensure_ascii=False))
    sys.exit(exit_code)


def load_config():
    """Read settings from .env / environment.
    :return: dict with the script configuration
    """
    load_dotenv(BASE_DIR / ".env")
    missing = [k for k in ("ODOO_URL", "ODOO_LOGIN", "ODOO_PASSWORD") if not os.getenv(k)]
    if missing:
        emit("error", f"missing_env:{','.join(missing)}", EXIT_ERROR)
    return {
        "url": os.environ["ODOO_URL"].rstrip("/"),
        "db": os.getenv("ODOO_DB", ""),
        "login": os.environ["ODOO_LOGIN"],
        "password": os.environ["ODOO_PASSWORD"],
        "tz": ZoneInfo(os.getenv("TZ_NAME", "Europe/Madrid")),
        "jitter_max": float(os.getenv("JITTER_MAX_MINUTES", "0")),
        "min_toggle": float(os.getenv("MIN_TOGGLE_MINUTES", "60")),
        "post_login_pause": float(os.getenv("POST_LOGIN_PAUSE_SECONDS", "3")),
        "systray_selector": os.getenv("SYSTRAY_SELECTOR") or ".o_menu_systray > button.dropdown-toggle:has(> i.fa-circle)",
    }


def utc_day_bounds(day, tz):
    """Return the local day as UTC strings in Odoo format.
    :param day: local date
    :param tz: local ZoneInfo
    :return: (start_utc, end_utc) strings
    """
    start = datetime.combine(day, dtime.min, tz).astimezone(timezone.utc)
    end = datetime.combine(day, dtime.max, tz).astimezone(timezone.utc)
    return start.strftime(ODOO_DT), end.strftime(ODOO_DT)


def rpc(page, base_url, route, params):
    """Call an Odoo JSON-RPC route reusing the browser session cookie.
    :param page: logged-in Playwright page
    :param base_url: Odoo base URL
    :param route: path such as /web/dataset/call_kw
    :param params: JSON-RPC params
    :return: the 'result' value
    """
    resp = page.request.post(
        f"{base_url}{route}",
        data={"jsonrpc": "2.0", "method": "call", "params": params, "id": random.randint(1, 10**9)},
    )
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(body["error"].get("data", {}).get("message") or body["error"])
    return body["result"]


def call_kw(page, base_url, model, method, args, **kwargs):
    """Call a model method through /web/dataset/call_kw.
    :param model: Odoo model name
    :param method: public method name
    :param args: positional args
    :param kwargs: keyword args
    """
    return rpc(page, base_url, f"/web/dataset/call_kw/{model}/{method}",
               {"model": model, "method": method, "args": args, "kwargs": kwargs})


def create_attendance_context(browser, base_url):
    """Create a fresh browser context without implicit location access.
    :param browser: Playwright Chromium browser
    :param base_url: configured Odoo base URL
    :return: isolated browser context
    """
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("ODOO_URL must include an http(s) origin")
    return browser.new_context()


def query_geoclue_location():
    """Read a fresh GeoClue fix via system Python; never log raw coordinates.
    :return: Playwright geolocation dict, or None when GeoClue is unavailable
    """
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-c", GEOCLUE_QUERY],
            capture_output=True,
            text=True,
            timeout=25,
            check=True,
        )
        position = json.loads(result.stdout)
        latitude = position["latitude"]
        longitude = position["longitude"]
        accuracy = position["accuracy"]
        values = (latitude, longitude, accuracy)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in values):
            return None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and accuracy >= 0):
            return None
        return {"latitude": latitude, "longitude": longitude, "accuracy": accuracy}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def configure_attendance_geolocation(context, base_url, geolocation):
    """Inject a fresh fix and grant location permission only to the Odoo origin.
    :return: True if configured, False if no fix is available
    """
    if geolocation is None:
        return False
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("ODOO_URL must include an http(s) origin")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    context.set_geolocation(geolocation)
    context.grant_permissions(["geolocation"], origin=origin)
    return True


def verify_page_geolocation(page, geolocation):
    """Check navigator.geolocation against the injected fix without returning coordinates."""
    if geolocation is None:
        return {"ok": False, "reason": "geoclue_unavailable"}
    expected = {key: geolocation[key] for key in ("latitude", "longitude")}
    try:
        return page.evaluate("""expected => new Promise(resolve => {
          if (!navigator.geolocation) return resolve({ok:false, reason:'api_unavailable'});
          navigator.geolocation.getCurrentPosition(
            p => resolve({
              ok:true,
              matches_injected:
                Math.abs(p.coords.latitude-expected.latitude) < 1e-7 &&
                Math.abs(p.coords.longitude-expected.longitude) < 1e-7,
              accuracy_m:Math.round(p.coords.accuracy)
            }),
            e => resolve({ok:false, error_code:e.code}),
            {enableHighAccuracy:true, timeout:12000, maximumAge:0}
          );
        })""", expected)
    except Exception:
        return {"ok": False, "reason": "browser_geolocation_error"}


def login(page, cfg):
    """Log in through the /web/login form and wait for the web client.
    :param page: Playwright page
    :param cfg: configuration dict
    """
    url = f"{cfg['url']}/web/login" + (f"?db={cfg['db']}" if cfg["db"] else "")
    page.goto(url)
    page.fill("input[name='login']", cfg["login"])
    page.fill("input[name='password']", cfg["password"])
    page.click("form button[type='submit']")
    page.wait_for_selector(".o_main_navbar", timeout=30000)
    time.sleep(cfg["post_login_pause"])


def get_employee(page, cfg):
    """Return the hr.employee record of the logged-in user.
    :return: dict with id, name and resource_calendar_id
    """
    uid = rpc(page, cfg["url"], "/web/session/get_session_info", {})["uid"]
    emps = call_kw(page, cfg["url"], "hr.employee", "search_read",
                   [[("user_id", "=", uid)]], fields=["name", "resource_calendar_id"], limit=1)
    if not emps:
        emit("error", "no_employee_for_user", EXIT_ERROR)
    return emps[0]


def fetch_off_periods(page, cfg, employee, first_day, last_day):
    """Fetch approved leaves and public holidays overlapping a local date range.
    :param employee: hr.employee dict
    :param first_day: first local date (inclusive)
    :param last_day: last local date (inclusive)
    :return: (leaves, holidays) lists of records
    """
    start, _ = utc_day_bounds(first_day, cfg["tz"])
    _, end = utc_day_bounds(last_day, cfg["tz"])
    leaves = call_kw(page, cfg["url"], "hr.leave", "search_read", [[
        ("employee_id", "=", employee["id"]), ("state", "=", "validate"),
        ("date_from", "<=", end), ("date_to", ">=", start),
    ]], fields=["holiday_status_id", "date_from", "date_to"], order="date_from")
    calendar_id = employee["resource_calendar_id"] and employee["resource_calendar_id"][0]
    holidays = call_kw(page, cfg["url"], "resource.calendar.leaves", "search_read", [[
        ("resource_id", "=", False), ("calendar_id", "in", [calendar_id, False]),
        ("date_from", "<=", end), ("date_to", ">=", start),
    ]], fields=["name", "date_from", "date_to"], order="date_from")
    return leaves, holidays


def find_skip_reason(page, cfg, employee, day):
    """Check approved leaves and public holidays touching the given day.
    :param employee: hr.employee dict
    :param day: local date
    :return: reason string or None
    """
    leaves, holidays = fetch_off_periods(page, cfg, employee, day, day)
    if leaves:
        return f"leave:{leaves[0]['holiday_status_id'][1]}"
    if holidays:
        return f"holiday:{holidays[0]['name']}"
    return None


def to_local(value, tz):
    """Convert an Odoo UTC datetime string to a local 'YYYY-MM-DD HH:MM' string.
    :param value: Odoo datetime string
    :param tz: local ZoneInfo
    """
    dt = datetime.strptime(value, ODOO_DT).replace(tzinfo=timezone.utc).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M")


def print_off_days(page, cfg, employee, first_day, days):
    """Print leaves and holidays the script would skip in the next N days.
    :param first_day: first local date
    :param days: number of days to look ahead
    """
    last_day = first_day + timedelta(days=days)
    leaves, holidays = fetch_off_periods(page, cfg, employee, first_day, last_day)
    cal = employee["resource_calendar_id"]
    print(f"Empleado: {employee['name']} | Calendario: {cal[1] if cal else '-'}")
    print(f"Rango: {first_day} -> {last_day}\n")
    print(f"Ausencias aprobadas ({len(leaves)}):")
    for lv in leaves:
        print(f"  {to_local(lv['date_from'], cfg['tz'])} -> {to_local(lv['date_to'], cfg['tz'])}  {lv['holiday_status_id'][1]}")
    print(f"\nFestivos ({len(holidays)}):")
    for hd in holidays:
        print(f"  {to_local(hd['date_from'], cfg['tz'])} -> {to_local(hd['date_to'], cfg['tz'])}  {hd['name']}")


def last_attendance(page, cfg, employee):
    """Return the most recent hr.attendance of the employee, or None.
    :param employee: hr.employee dict
    """
    recs = call_kw(page, cfg["url"], "hr.attendance", "search_read",
                   [[("employee_id", "=", employee["id"])]],
                   fields=["check_in", "check_out"], order="check_in desc", limit=1)
    return recs[0] if recs else None


def wait_for_attendance_transition(fetch, was_open, timeout=20, poll_interval=0.5):
    """Poll Odoo until the latest attendance changes open/closed state.
    :param fetch: callable returning the latest attendance record
    :param was_open: whether the record was open before the UI click
    :param timeout: maximum seconds to wait for Odoo to persist the change
    :param poll_interval: seconds between reads
    :return: the transitioned record, or None if state did not change
    """
    deadline = time.monotonic() + timeout
    while True:
        current = fetch()
        is_open = bool(current and not current.get("check_out"))
        if is_open != was_open:
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval, remaining))


def minutes_since_last_toggle(att):
    """Minutes elapsed since the last check-in/out of an attendance record.
    :param att: hr.attendance dict or None
    :return: float minutes, or None if there is no record
    """
    if not att:
        return None
    last = datetime.strptime(att["check_out"] or att["check_in"], ODOO_DT).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 60


def accept_location_fallback_dialog(page, timeout_ms=11000):
    """Accept only Odoo's explicit no-valid-location confirmation.
    :param page: logged-in Playwright page
    :param timeout_ms: maximum wait for the geolocation fallback dialog
    :return: True if the location fallback was accepted, otherwise False
    """
    location_error = re.compile(
        r"no se puede obtener una ubicación válida|could not get a valid location|could not obtain a valid location",
        re.IGNORECASE,
    )
    dialog = page.get_by_role("dialog").filter(has_text=location_error)
    try:
        dialog.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False

    continue_label = re.compile(r"^(continuar de todos modos|continue anyway)$", re.IGNORECASE)
    button = dialog.get_by_role("button", name=continue_label)
    matches = button.count()
    if matches != 1:
        raise RuntimeError(f"location_fallback_button_matches:{matches}")
    button.click()
    return True


def click_systray(page, cfg, action):
    """Open the attendance systray and click the check-in/out button.
    :param page: logged-in Playwright page
    :param cfg: configuration dict
    :param action: check_in | check_out, used to pick the button icon
    """
    toggle = page.locator(cfg["systray_selector"])
    if toggle.count() != 1:
        raise RuntimeError(f"systray_toggle_matches:{toggle.count()}")
    toggle.click()
    icon = "fa-sign-out" if action == "check_out" else "fa-sign-in"
    button = page.locator(f".o_att_menu_container button.btn:has(i.{icon})")
    button.wait_for(state="visible", timeout=10000)
    button.click()
    accept_location_fallback_dialog(page)


def main():
    """Entry point: decide whether to skip, then toggle attendance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="compute the decision without clicking")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--no-jitter", action="store_true", help="ignore JITTER_MAX_MINUTES")
    parser.add_argument("--expect", choices=["in", "out"], help="only act if the pending action is check_<in|out>")
    parser.add_argument("--list-offdays", type=int, metavar="DAYS", help="print leaves/holidays in the next DAYS and exit")
    parser.add_argument("--date", type=date.fromisoformat, help="simulate another day YYYY-MM-DD (forces --dry-run)")
    opts = parser.parse_args()
    cfg = load_config()

    real_today = datetime.now(cfg["tz"]).date()
    today = opts.date or real_today
    if today != real_today:
        opts.dry_run = True
    if today.weekday() >= 5 and opts.list_offdays is None:
        emit("skipped", "weekend", EXIT_SKIPPED, date=today)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not opts.headed)
        context = create_attendance_context(browser, cfg["url"])
        page = context.new_page()
        try:
            login(page, cfg)
            employee = get_employee(page, cfg)
            if opts.list_offdays is not None:
                print_off_days(page, cfg, employee, today, opts.list_offdays)
                sys.exit(EXIT_DONE)
            reason = find_skip_reason(page, cfg, employee, today)
            if reason:
                emit("skipped", reason, EXIT_SKIPPED, date=today)

            att = last_attendance(page, cfg, employee)
            is_open = bool(att and not att["check_out"])
            action = "check_out" if is_open else "check_in"
            elapsed = minutes_since_last_toggle(att)
            if elapsed is not None and elapsed < cfg["min_toggle"]:
                emit("skipped", "recent_toggle", EXIT_SKIPPED, minutes_since=round(elapsed, 1), pending=action)
            if opts.expect and action != f"check_{opts.expect}":
                emit("skipped", "unexpected_state", EXIT_SKIPPED, expected=f"check_{opts.expect}", would_do=action)
            if opts.dry_run:
                location = query_geoclue_location()
                configured = configure_attendance_geolocation(context, cfg["url"], location)
                location_check = verify_page_geolocation(page, location if configured else None)
                emit(
                    "skipped", "dry_run", EXIT_SKIPPED,
                    would_do=action,
                    employee=employee["name"],
                    geolocation=location_check,
                )

            if cfg["jitter_max"] > 0 and not opts.no_jitter:
                time.sleep(random.uniform(0, cfg["jitter_max"] * 60))

            location = query_geoclue_location()
            configure_attendance_geolocation(context, cfg["url"], location)
            click_systray(page, cfg, action)
            after = wait_for_attendance_transition(
                lambda: last_attendance(page, cfg, employee), was_open=is_open
            )
            if after is None:
                current = last_attendance(page, cfg, employee)
                try:
                    page.screenshot(path=str(BASE_DIR / "last_error.png"))
                except Exception:
                    pass
                detail = (
                    "Odoo did not reflect the attendance state change within 20 seconds; "
                    f"latest check_in={current.get('check_in') if current else None}, "
                    f"check_out={current.get('check_out') if current else None}"
                )
                emit("error", f"{action}_not_registered", EXIT_ERROR, detail=detail)
            emit("done", action, EXIT_DONE, employee=employee["name"], at=datetime.now(cfg["tz"]).strftime("%H:%M"))
        except SystemExit:
            raise
        except Exception as exc:
            page.screenshot(path=str(BASE_DIR / "last_error.png"))
            emit("error", "exception", EXIT_ERROR, detail=str(exc))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
