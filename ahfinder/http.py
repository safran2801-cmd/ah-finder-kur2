"""HTTP-Wrapper um `requests` mit Parallel-Fetch (ersetzt curl_multi).

Enthaelt einfaches Retry-mit-Backoff fuer transiente Fehler (Timeouts,
Verbindungsfehler, 429/5xx). Das ist wichtig fuer Wikipedia/Wikidata-Abrufe:
unter Last (z.B. viele parallele Requests vom curator.py-Job) drosselt
Wikimedia gerne kurzzeitig (429) oder antwortet langsam/garnicht - ein
einzelner Fehlversuch soll dann nicht sofort als "kein Ergebnis" gewertet
werden, sondern nach kurzer Pause erneut versucht werden.
"""
from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping, Optional

import requests

USER_AGENT = (
    "AlpenHuettenFinder/1.0 "
    "(https://github.com/; kontakt ueber GitHub Issues; "
    "python-requests)"
)

# Status-Codes, bei denen sich ein erneuter Versuch lohnt (Drosselung/
# voruebergehende Serverfehler) - alles andere (200, 404, ...) wird sofort
# zurueckgegeben.
_RETRYABLE_CODES = {429, 500, 502, 503, 504}


def _headers(extra: Optional[Mapping[str, str]] = None) -> dict:
    h = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if extra:
        h.update(extra)
    return h


def _sleep_backoff(attempt: int, retry_after: Optional[str] = None) -> None:
    """Wartet vor dem naechsten Versuch - respektiert ggf. Retry-After,
    sonst exponentielles Backoff mit etwas Jitter."""
    wait = (2 ** attempt) * 0.6
    if retry_after:
        try:
            wait = max(wait, float(retry_after))
        except ValueError:
            pass
    wait += random.uniform(0, 0.4)
    time.sleep(min(wait, 8.0))


def http_get(
    url: str,
    timeout: int = 15,
    headers: Optional[Mapping[str, str]] = None,
    retries: int = 2,
) -> dict:
    last_error = ""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=(min(10, timeout), timeout), headers=_headers(headers))
            if r.status_code in _RETRYABLE_CODES and attempt < retries:
                _sleep_backoff(attempt, r.headers.get("Retry-After"))
                continue
            return {"code": r.status_code, "body": r.text, "error": ""}
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            return {"code": 0, "body": None, "error": last_error}
    return {"code": 0, "body": None, "error": last_error}


def http_post(
    url: str,
    payload: Any,
    timeout: int = 30,
    headers: Optional[Mapping[str, str]] = None,
    retries: int = 1,
) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    h = _headers(headers)
    if "Content-Type" not in h:
        h["Content-Type"] = "application/json"
    last_error = ""
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=body, timeout=(min(10, timeout), timeout), headers=h)
            if r.status_code in _RETRYABLE_CODES and attempt < retries:
                _sleep_backoff(attempt, r.headers.get("Retry-After"))
                continue
            return {"code": r.status_code, "body": r.text, "error": ""}
        except requests.RequestException as e:
            last_error = str(e)
            if attempt < retries:
                _sleep_backoff(attempt)
                continue
            return {"code": 0, "body": None, "error": last_error}
    return {"code": 0, "body": None, "error": last_error}


def http_multi_get(items: Mapping[Any, str], timeout: int = 15, max_workers: int = 8) -> dict:
    """Parallele GET-Anfragen, gibt ein Dict {key -> Response} zurueck."""
    if not items:
        return {}
    results: dict = {}
    workers = min(max_workers, max(1, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(http_get, url, timeout): key for key, url in items.items()}
        for fut, key in futures.items():
            try:
                results[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[key] = {"code": 0, "body": None, "error": str(e)}
    return results
