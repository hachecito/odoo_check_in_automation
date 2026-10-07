"""Odoo 19 attendance check-in/check-out via Playwright, skipping weekends, leaves and holidays."""

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

EXIT_DONE, EXIT_ERROR, EXIT_SKIPPED = 0, 1, 10
ODOO_DT = "%Y-%m-%d %H:%M:%S"
BASE_DIR = Path(__file__).resolve().parent


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


def minutes_since_last_toggle(att):
    """Minutes elapsed since the last check-in/out of an attendance record.
    :param att: hr.attendance dict or None
    :return: float minutes, or None if there is no record
    """
    if not att:
        return None
    last = datetime.strptime(att["check_out"] or att["check_in"], ODOO_DT).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() / 60


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
    page.wait_for_timeout(2000)


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
        page = browser.new_page()
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
                emit("skipped", "dry_run", EXIT_SKIPPED, would_do=action, employee=employee["name"])

            if cfg["jitter_max"] > 0 and not opts.no_jitter:
                time.sleep(random.uniform(0, cfg["jitter_max"] * 60))

            click_systray(page, cfg, action)
            after = last_attendance(page, cfg, employee)
            if bool(after and not after["check_out"]) == is_open:
                emit("error", f"{action}_not_registered", EXIT_ERROR)
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
