"""
generic_calendar.py
====================

Live-Verfügbarkeitsabfrage für Hütten OHNE SAC-Tourenportal-Anbindung
(z.B. DAV-, ÖAV-, CAI- und FFCAM-Hütten), basierend auf derselben
Fingerprint-/Reader-Logik, die für den Zürcher Waldhütten-Finder
entwickelt wurde (siehe wh-finder2/availability_checker.py).

Im Unterschied zum Waldhütten-Projekt gibt es hier KEINE vorab von Hand
recherchierte "availability_check"-Konfiguration pro Hütte - statt-
dessen wird zur Laufzeit:

    1. die offizielle Website der Hütte (h["websiteUrl"], via
       website.find_official_website() ermittelt) per detect_system()
       auf bekannte Fingerprints geprüft, und
    2. bei einem Treffer mit per-HTTP abrufbarem System (raumkalender,
       jevents, joomla_fabrik_fullcalendar) sofort live ausgelesen.

Systeme, die ihre Belegung erst per JavaScript im Browser rendern
(wpbs, wpbc, calendarapp_de, jquery_ui_datepicker), werden erkannt,
aber nicht live ausgelesen (würde eine Headless-Browser-Engine wie
Playwright zur Laufzeit erfordern - noch nicht implementiert).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import requests


@dataclass
class AvailabilityResult:
    hut_name: str
    system: Optional[str]
    supported: bool
    checked_at: str
    blocked_dates: list[str] = field(default_factory=list)
    error: Optional[str] = None
    raw: Optional[dict] = None

    def is_blocked_on(self, day: date) -> Optional[bool]:
        if self.error or not self.supported:
            return None
        return day.isoformat() in self.blocked_dates


@dataclass
class SystemDetection:
    checked_url: str
    system: Optional[str]
    confidence: str  # "high" | "medium" | "none"
    evidence: list[str] = field(default_factory=list)
    candidate_params: dict = field(default_factory=dict)
    other_matches: list[str] = field(default_factory=list)
    requires_browser: bool = False
    error: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month_range(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


# ── Fingerprints der bekannten Kalendersysteme ──────────────────────────────

_FINGERPRINTS: list[tuple[str, list[tuple[re.Pattern, str]], bool]] = [
    (
        "raumkalender",
        [(re.compile(r"raumkalender\.com", re.I), "URL/Skript verweist auf raumkalender.com")],
        False,
    ),
    (
        "joomla_fabrik_fullcalendar",
        [
            (re.compile(r"com_fabrik", re.I), "Joomla-Komponente 'com_fabrik' gefunden"),
            (re.compile(r"fabrik[._-]?fullcalendar", re.I), "Fabrik-FullCalendar-Visualisierung gefunden"),
        ],
        False,
    ),
    (
        "jevents",
        [
            (re.compile(r"com_jevents", re.I), "Joomla-Komponente 'com_jevents' gefunden"),
            (re.compile(r"cal_td_days(hasevents|withnoevents)", re.I), "JEvents-Kalenderzellen-Klassen gefunden"),
            (re.compile(r"month\.calendar/\d{4}/\d{2}", re.I), "JEvents-Monatsansicht-Link gefunden"),
        ],
        False,
    ),
    (
        "wpbs",
        [
            (re.compile(r"wp-booking-system", re.I), "WordPress-Plugin 'WP Booking System' (wpbs) gefunden"),
            (re.compile(r"\bwpbs[_-]", re.I), "wpbs-Skript/Asset-Namen gefunden"),
        ],
        True,
    ),
    (
        "wpbc",
        [
            (re.compile(r"wp-booking-calendar", re.I), "WordPress-Plugin 'WP Booking Calendar' (wpbc) gefunden"),
            (re.compile(r"\bwpbc[_-]", re.I), "wpbc-Skript/Asset-Namen gefunden"),
        ],
        True,
    ),
    (
        "calendarapp_de",
        [
            (re.compile(r"calendarapp\.de", re.I), "Skript/Endpoint von app.calendarapp.de gefunden"),
            (re.compile(r'id="zhcal-root"', re.I), "zhcal-root-Widget-Container gefunden"),
        ],
        True,
    ),
    (
        "jquery_ui_datepicker",
        [
            (re.compile(r"ui-datepicker", re.I), "jQuery-UI-Datepicker-CSS-Klasse gefunden"),
            (re.compile(r"jquery-ui[^\"']*\.(js|css)", re.I), "jQuery-UI-Asset eingebunden"),
        ],
        True,
    ),
]


def _extract_candidate_params(system: str, html: str, base_url: str = "") -> dict:
    params: dict = {}
    if system == "raumkalender":
        m = re.search(r"resource/(\d+)", html)
        if m:
            params["resource_id"] = m.group(1)
    elif system == "joomla_fabrik_fullcalendar":
        for key in ("visualizationid", "listid", "Itemid"):
            m = re.search(rf"{key}=(\d+)", html)
            if m:
                params[key] = int(m.group(1))
    elif system == "jevents":
        m = re.search(r'href="([^"]*?)/month\.calendar/\d{4}/\d{2}', html)
        if m:
            params["calendar_url"] = urljoin(base_url, m.group(1)) if base_url else m.group(1)
    return params


def detect_system(url: str) -> SystemDetection:
    """Lädt die Seite per HTTP-GET und prüft sie auf bekannte Kalender-Fingerprints.

    Rein heuristisch: kein Treffer heisst nicht zwingend, dass kein Online-
    Kalender existiert (z.B. wenn er erst auf einer Unterseite eingebunden ist,
    oder ein unbekanntes System verwendet wird).
    """
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        return SystemDetection(url, None, "none", error=str(e))

    matches: list[tuple[str, list[str], bool]] = []
    for system, patterns, needs_browser in _FINGERPRINTS:
        evidence = [text for pattern, text in patterns if pattern.search(html)]
        if evidence:
            matches.append((system, evidence, needs_browser))

    if not matches:
        return SystemDetection(
            url, None, "none",
            evidence=["Keine bekannten Kalender-Fingerprints im HTML gefunden."],
        )

    matches.sort(key=lambda m: len(m[1]), reverse=True)
    best_system, best_evidence, needs_browser = matches[0]
    confidence = "high" if len(best_evidence) >= 2 else "medium"
    candidate_params = {} if needs_browser else _extract_candidate_params(best_system, html, url)

    return SystemDetection(
        checked_url=url,
        system=best_system,
        confidence=confidence,
        evidence=best_evidence,
        candidate_params=candidate_params,
        other_matches=[m[0] for m in matches[1:]],
        requires_browser=needs_browser,
    )


# ── Live-Reader für die per HTTP abrufbaren Systeme ─────────────────────────

def _check_raumkalender(hut_name: str, resource_id: str, start: date, end: date) -> AvailabilityResult:
    url = f"https://app.raumkalender.com/public/resource/{resource_id}/availability"
    params = {"start": start.isoformat(), "end": end.isoformat()}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return AvailabilityResult(hut_name, "raumkalender", True, _now(), error=str(e))

    blocked = []

    def _add_key(key) -> None:
        try:
            y, m, d = str(key).split("-")
            blocked.append(date(int(y), int(m), int(d)).isoformat())
        except (ValueError, AttributeError):
            pass

    if isinstance(data, dict):
        for key in sorted(data.keys()):
            _add_key(key)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                _add_key(item)
            elif isinstance(item, (list, tuple)) and item:
                _add_key(item[0])
            elif isinstance(item, dict):
                _add_key(item.get("date") or item.get("day") or item.get("datum"))
    else:
        return AvailabilityResult(
            hut_name, "raumkalender", True, _now(),
            error=f"Unerwartetes Antwortformat von raumkalender (Typ {type(data).__name__}).",
        )

    return AvailabilityResult(
        hut_name, "raumkalender", True, _now(),
        blocked_dates=sorted(set(blocked)), raw={"type": type(data).__name__},
    )


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _check_joomla_fabrik(hut_name: str, params_in: dict, start: date, end: date) -> AvailabilityResult:
    url = params_in.get("base_url") or ""
    if not url:
        return AvailabilityResult(
            hut_name, "joomla_fabrik_fullcalendar", True, _now(),
            error="Keine Fabrik-Endpoint-URL ermittelt.",
        )
    params = {
        "format": "raw",
        "Itemid": params_in.get("Itemid", ""),
        "visualizationid": params_in.get("visualizationid", ""),
        "listid": params_in.get("listid", ""),
        "eventListKey": 0,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        cleaned = _CONTROL_CHARS.sub("", r.text)
        data = json.loads(cleaned)
    except Exception as e:
        return AvailabilityResult(hut_name, "joomla_fabrik_fullcalendar", True, _now(), error=str(e))

    blocked = set()
    for entry in data.values():
        raw_date = entry.get("reservation___datum") or entry.get("reservation___datum_raw")
        if not raw_date:
            continue
        try:
            d = datetime.strptime(raw_date.split(" ")[0], "%Y-%m-%d").date()
            blocked.add(d.isoformat())
        except ValueError:
            continue
    return AvailabilityResult(
        hut_name, "joomla_fabrik_fullcalendar", True, _now(),
        blocked_dates=sorted(blocked), raw={"entries": len(data)},
    )


_JEVENTS_DAY_RE = re.compile(
    r'cal_td_dayshasevents"[^>]*>\s*<a[^>]*href="[^"]*day\.listevents/'
    r'(\d{4})/(\d{2})/(\d{2})'
)


def _check_jevents(hut_name: str, calendar_url: str, start: date, end: date) -> AvailabilityResult:
    base_url = calendar_url.rstrip("/")
    blocked = set()
    try:
        for y, m in _month_range(start, end):
            url = f"{base_url}/month.calendar/{y}/{m:02d}/01/-.html"
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            for ys, ms, ds in _JEVENTS_DAY_RE.findall(r.text):
                try:
                    d = date(int(ys), int(ms), int(ds))
                except ValueError:
                    continue
                if start <= d <= end:
                    blocked.add(d.isoformat())
    except Exception as e:
        return AvailabilityResult(hut_name, "jevents", True, _now(), error=str(e))

    return AvailabilityResult(hut_name, "jevents", True, _now(), blocked_dates=sorted(blocked))


_BROWSER_REQUIRED_NOTE = (
    "Diese Hütte nutzt vermutlich das Kalendersystem '{system}' (Konfidenz: {confidence}), "
    "das die Belegung erst per JavaScript im Browser rendert. Ein einfacher HTTP-Abruf "
    "liefert nur ein leeres Gerüst - ein Live-Check würde eine Headless-Browser-Engine "
    "(z.B. Playwright) benötigen, die hier noch nicht zur Verfügung steht."
)


def check_via_website(hut_name: str, website_url: str, start: date, end: date) -> AvailabilityResult:
    """Erkennt das Kalendersystem der offiziellen Website einer Hütte live und
    liest - falls per HTTP möglich - direkt die Belegungsdaten aus.

    Gedacht für Hütten ohne SAC-Tourenportal-Eintrag (DAV/ÖAV/CAI/FFCAM etc.),
    deren Website-URL bereits über website.find_official_website() bekannt ist.
    """
    if not website_url:
        return AvailabilityResult(
            hut_name, None, False, _now(),
            error="Keine offizielle Website für diese Hütte bekannt - keine Kalender-Erkennung möglich.",
        )

    detection = detect_system(website_url)

    if detection.error:
        return AvailabilityResult(
            hut_name, None, False, _now(),
            error=f"Website nicht abrufbar ({detection.error}).",
        )

    if not detection.system:
        return AvailabilityResult(
            hut_name, None, False, _now(),
            error="Auf der Website wurde kein bekanntes Online-Kalendersystem erkannt.",
        )

    if detection.requires_browser:
        return AvailabilityResult(
            hut_name, detection.system, True, _now(),
            error=_BROWSER_REQUIRED_NOTE.format(
                system=detection.system, confidence=detection.confidence,
            ),
        )

    if detection.system == "raumkalender":
        resource_id = detection.candidate_params.get("resource_id")
        if not resource_id:
            return AvailabilityResult(
                hut_name, "raumkalender", True, _now(),
                error="raumkalender erkannt, aber resource_id konnte nicht ermittelt werden.",
            )
        return _check_raumkalender(hut_name, resource_id, start, end)

    if detection.system == "jevents":
        calendar_url = detection.candidate_params.get("calendar_url")
        if not calendar_url:
            return AvailabilityResult(
                hut_name, "jevents", True, _now(),
                error="JEvents erkannt, aber Kalender-URL konnte nicht ermittelt werden.",
            )
        return _check_jevents(hut_name, calendar_url, start, end)

    if detection.system == "joomla_fabrik_fullcalendar":
        params = dict(detection.candidate_params)
        params["base_url"] = website_url
        return _check_joomla_fabrik(hut_name, params, start, end)

    return AvailabilityResult(
        hut_name, detection.system, True, _now(),
        error=f"Kein Live-Reader für System '{detection.system}' implementiert.",
    )
