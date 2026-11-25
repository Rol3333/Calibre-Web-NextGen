# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""HTTP access to Open Library edition JSON and the original MARC records.

This is the single place every Open Library request goes through, so the
exception contract, the rate limiter and the response validation are shared by
the foreground provider and the background save task alike.

Exception contract
------------------
``OLUnavailable`` means "the request did not produce a usable answer and may be
retried": a timeout, a connection error, a rate-limit (429) or a 5xx. A 404
means "no such edition" and returns ``None``. A malformed body or a permanent
client error (any other 4xx) raises ``ValueError`` — that is a data/format
problem, not a transient outage, and must not be retried as one.

Rate limiting
-------------
Open Library asks for at most one request per second per client. The limiter
lives here (not in the task) so the provider and every task instance share it.
It is thread-safe within one process; it does not coordinate separate OS
processes that share one public IP, so a multi-process deployment should either
run a single worker or accept the conservative 1.1 s interval per process.
"""

import random
import re
import threading
import time
from functools import lru_cache

import requests

from cps import constants

HEADERS = {"User-Agent": constants.USER_AGENT}   # add a contact email later for 3 req/s
_LOC = re.compile(r"^marc:([^/]+)/([^:]+):(\d+):(\d+)$")

SEARCH_URL = "https://openlibrary.org/search.json"
SEARCH_TIMEOUT = (10, 30)
SEARCH_MAX_ATTEMPTS = 3
SEARCH_BACKOFF_BASE = 1.0
SEARCH_BACKOFF_MAX = 8.0

# One request per this many seconds, shared by every caller in this process.
MIN_INTERVAL = 1.1
# Archive.org is a different service with its own policy; keep its throttle
# separate from the Open Library one.
ARCHIVE_MIN_INTERVAL = 1.1

_RATE_LOCK = threading.Lock()
_LAST_OL_CALL = 0.0
_LAST_ARCHIVE_CALL = 0.0


class OLUnavailable(Exception):
    """Timeout / connection error / 403 / 429 / 5xx — retryable."""


def _throttle(last_call, min_interval):
    """Block until ``min_interval`` has elapsed since ``last_call``; return now."""
    wait = min_interval - (time.monotonic() - last_call)
    if wait > 0:
        time.sleep(wait)
    return time.monotonic()


def _reserve_ol_slot():
    global _LAST_OL_CALL
    with _RATE_LOCK:
        _LAST_OL_CALL = _throttle(_LAST_OL_CALL, MIN_INTERVAL)
        return _LAST_OL_CALL


def _reserve_archive_slot():
    global _LAST_ARCHIVE_CALL
    with _RATE_LOCK:
        _LAST_ARCHIVE_CALL = _throttle(_LAST_ARCHIVE_CALL, ARCHIVE_MIN_INTERVAL)
        return _LAST_ARCHIVE_CALL


def _retry_after_seconds(response):
    """Parse the Retry-After header (seconds or an HTTP date), or None."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _jittered_backoff(delay):
    """Exponential backoff with jitter, capped at SEARCH_BACKOFF_MAX."""
    return min(delay, SEARCH_BACKOFF_MAX) * (0.5 + random.random())


def search_books(query, fields, limit):
    """Search Open Library through the shared HTTP service.

    ``query`` is the URL-encoded ``q`` value (the provider builds it from its
    title tokens). Uses the shared rate limiter, explicit timeouts, bounded
    retries with exponential backoff + jitter for connection errors, timeouts,
    429 and transient 5xx, and honours ``Retry-After``. Permanent 4xx errors
    and malformed JSON raise ``ValueError`` (not retried); a retryable failure
    that exhausts the attempts raises ``OLUnavailable`` so the caller can
    distinguish "no results" from "the source failed".
    """
    url = f"{SEARCH_URL}?q={query}&fields={fields}&limit={limit}"
    delay = SEARCH_BACKOFF_BASE
    for attempt in range(SEARCH_MAX_ATTEMPTS):
        _reserve_ol_slot()
        try:
            r = requests.get(url, headers=HEADERS, timeout=SEARCH_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == SEARCH_MAX_ATTEMPTS - 1:
                raise OLUnavailable(type(exc).__name__) from exc
            time.sleep(_jittered_backoff(delay))
            delay *= 2
            continue
        try:
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == SEARCH_MAX_ATTEMPTS - 1:
                    raise OLUnavailable(f"HTTP {r.status_code}")
                retry_after = _retry_after_seconds(r)
                time.sleep(retry_after if retry_after is not None else _jittered_backoff(delay))
                delay *= 2
                continue
            # Any other 4xx is permanent: raise_for_status raises and we do not
            # retry it.
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise OLUnavailable(type(exc).__name__) from exc
        except ValueError as exc:
            raise ValueError(f"invalid search JSON: {exc}") from exc
        finally:
            r.close()
    raise OLUnavailable("search failed")


@lru_cache(maxsize=128)
def fetch_edition(isbn13):
    _reserve_ol_slot()
    try:
        r = requests.get(f"https://openlibrary.org/isbn/{isbn13}.json",
                         headers=HEADERS, timeout=(10, 30))
    except requests.RequestException as exc:
        raise OLUnavailable(type(exc).__name__) from exc
    try:
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429) or r.status_code >= 500:
            raise OLUnavailable(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        raise OLUnavailable(type(exc).__name__) from exc
    except ValueError as exc:
        # Invalid JSON / wrong content type: a data problem, not an outage.
        raise ValueError(f"invalid edition JSON: {exc}") from exc
    finally:
        r.close()


def marc_locators(edition):
    return [m.groups() for s in edition.get("source_records", []) if (m := _LOC.match(s))]


@lru_cache(maxsize=128)
def fetch_marc(item, fname, off, ln):
    off, ln = int(off), int(ln)
    _reserve_archive_slot()
    try:
        r = requests.get(f"https://archive.org/download/{item}/{fname}",
                         headers={**HEADERS, "Range": f"bytes={off}-{off+ln-1}"},
                         stream=True, timeout=(10, 30))
    except requests.RequestException as exc:
        raise OLUnavailable(type(exc).__name__) from exc
    try:
        if r.status_code != 206:           # a 200 would mean the whole ~200 MB file
            raise OLUnavailable(f"archive.org HTTP {r.status_code}")
        data = r.content
        # Content-Range: "bytes 0-898/123456" — the served window must match the
        # requested one, or a proxy/redirect handed back a different slice.
        content_range = r.headers.get("Content-Range", "")
        m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range)
        if m and (int(m.group(1)) != off or int(m.group(2)) != off + ln - 1):
            raise ValueError(f"archive.org range mismatch: {content_range!r}")
        if len(data) != ln:
            raise ValueError(f"incomplete MARC record: {len(data)} != {ln}")
        if int(data[:5]) != len(data) or data[-1:] != b"\x1d":
            raise ValueError("incomplete MARC record")
        return data
    except requests.RequestException as exc:
        raise OLUnavailable(type(exc).__name__) from exc
    finally:
        r.close()


def parse_marc(data):
    """Binary MARC21 to {tag: [str | {subfield: [values]}]}. UTF-8 records only."""
    if len(data) < 25:
        raise ValueError("MARC record too short")
    if data[9:10] != b"a":                # leader/09: 'a' = UTF-8, blank = MARC-8
        raise ValueError("MARC-8 record: store the raw file, skip parsing")
    base = int(data[12:17])
    if not 25 <= base <= len(data):
        raise ValueError(f"bad MARC base address {base}")
    directory = data[24:base - 1]
    if len(directory) % 12:
        raise ValueError("MARC directory length not a multiple of 12")
    out = {}
    for i in range(0, len(directory), 12):
        tag = directory[i:i+3].decode("ascii", "replace")
        ln, st = int(directory[i+3:i+7]), int(directory[i+7:i+12])
        start = base + st
        if start < base or start + ln - 1 > len(data):
            raise ValueError(f"MARC field {tag} out of bounds")
        field = data[start: start + ln - 1]
        if tag < "010":
            out.setdefault(tag, []).append(field.decode("utf-8", "replace"))
        else:
            subs = {}
            for part in field[2:].split(b"\x1f"):
                if part:
                    subs.setdefault(chr(part[0]), []).append(part[1:].decode("utf-8", "replace"))
            out.setdefault(tag, []).append(subs)
    return out


def series_from_marc(rec):
    for tag in ("830", "490"):            # 830 = authorised form, 490 = as printed
        for f in rec.get(tag, []):
            name = re.sub(r"\s*\([^)]*\)", "", (f.get("a") or [""])[0]).strip(" ;,.:/")
            m = re.search(r"\d+(?:\.\d+)?", (f.get("v") or [""])[0])
            if name:
                return name, float(m.group()) if m else None
    return None, None