# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Save the Open Library search data for a book.

Modelled on :class:`cps.tasks.metadata_backup.TaskBackupMetadata`: a
``CalibreTask`` that runs in the worker thread, opens its own ``CalibreDB``
session and writes into the book's folder. It is queued after a successful
metadata apply — the auto-fetch in ``metadata_helper`` or a manual apply in
``editbooks.edit_book_param`` — so it runs whichever metadata provider won.
The saved data is keyed on the book's own ISBN, not on the provider.

This is *not* the library backup/archival workflow (``TaskBackupMetadata``,
which writes ``metadata.opf``). It only records what Open Library returned for
the book, so the lookup can be reproduced later without re-querying.

Layout written under ``<book_path>/<book.path>/data/openlibrary/``:

* ``edition.json``  — the raw Open Library edition object.
* ``marc_<n>_<item>_<offset>.mrc`` — one file per MARC record, ``<n>`` is the
  record's position (1-based), ``<item>``/``<offset>`` come from the
  ``source_records`` locator with the ``:``/``/`` stripped so the name is
  filesystem-safe.
* ``sources.json`` — one entry per written file with its ``locator``, the
  record's SHA-256 and the fetch date (UTC, ISO-8601).

The task is deliberately idempotent and polite: a book that already has
``edition.json`` is skipped, and at least ``MIN_INTERVAL`` seconds elapse
between Open Library calls. ``OLUnavailable`` (timeout / 403 / 429 / 5xx) is
retried with exponential back-off and finally logged, never raised, so one
rate-limited book cannot fail the whole run.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

from cps import config, db, logger
from cps.services.isbn import isbn13 as normalize_isbn
from cps.services.openlibrary_edition import (
    OLUnavailable,
    fetch_edition,
    fetch_marc,
    marc_locators,
)
from cps.services.worker import CalibreTask
from flask_babel import lazy_gettext as N_

log = logger.create()

# Open Library asks for <= 1 request/second; stay just above it.
MIN_INTERVAL = 1.1
# Cap on a single back-off wait, and on the attempts per request.
MAX_BACKOFF = 8
MAX_ATTEMPTS = 4


def _safe(value: str) -> str:
    """Strip the characters that are illegal or awkward in a file name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "")


def _book_isbn(book) -> str:
    """The book's first checksum-valid ISBN-13, or ''."""
    for identifier in getattr(book, "identifiers", []) or []:
        if (identifier.type or "").lower() in ("isbn", "isbn13", "isbn_13", "isbn10", "isbn_10"):
            isbn = normalize_isbn(identifier.val)
            if isbn:
                return isbn
    return ""


class TaskSaveOpenLibrarySearch(CalibreTask):
    """Write the Open Library edition + MARC records into the book folder."""

    def __init__(self, book_id=None, task_message=N_("Saving Open Library search data")):
        super().__init__(task_message)
        self.book_id = book_id
        self.log = logger.create()
        self._last_call = 0.0

    # -- CalibreTask API ---------------------------------------------------

    def run(self, worker_thread):
        self.calibre_db = db.CalibreDB(expire_on_commit=False, init=True)
        try:
            if self.book_id is not None:
                book = self.calibre_db.get_book(self.book_id)
                self._save_book(book)
            else:
                self._save_all()
            self._handleSuccess()
        except Exception as ex:  # noqa: BLE001 — task must not crash the worker
            self.log.error("Error saving Open Library search data: %s", ex)
            self._handleError(str(ex))
        finally:
            if self.calibre_db is not None and self.calibre_db.session is not None:
                self.calibre_db.session.close()

    @property
    def name(self):
        return "Open Library search data"

    @property
    def is_cancellable(self):
        return True

    def __str__(self):
        if self.book_id is not None:
            return f"Save Open Library search data for book {self.book_id}"
        return "Save Open Library search data for all books"

    # -- work --------------------------------------------------------------

    def _save_all(self):
        books = self.calibre_db.session.query(db.Books).all()
        count = len(books) or 1
        for i, book in enumerate(books, start=1):
            self._save_book(book)
            self.progress = i / count

    def _save_book(self, book):
        if book is None:
            return
        isbn = _book_isbn(book)
        if not isbn:
            self.log.debug("Book %s has no valid ISBN; skipping Open Library save", getattr(book, "id", "?"))
            return

        target = os.path.join(config.get_book_path(), book.path, "data", "openlibrary")
        edition_path = os.path.join(target, "edition.json")
        if os.path.exists(edition_path):
            self.log.debug("Book %s already has saved Open Library data; skipping", book.id)
            return

        try:
            edition = self._get_edition(isbn)
        except OLUnavailable as exc:
            self.log.warning("Open Library unavailable for %s: %s", isbn, exc)
            return
        if not edition:
            self.log.info("No Open Library edition for ISBN %s (book %s)", isbn, book.id)
            return

        os.makedirs(target, exist_ok=True)
        sources = [{
            "file": "edition.json",
            "locator": f"isbn:{isbn}",
            "sha256": self._write_bytes(edition_path, self._dumps(edition).encode("utf-8")),
            "fetched_at": self._now(),
        }]

        for n, (item, fname, off, ln) in enumerate(marc_locators(edition), start=1):
            try:
                data = self._get_marc(item, fname, off, ln)
            except (OLUnavailable, ValueError) as exc:
                self.log.warning("MARC %s/%s@%s unavailable: %s", item, fname, off, exc)
                continue
            name = f"marc_{n}_{_safe(item)}_{_safe(off)}.mrc"
            path = os.path.join(target, name)
            sources.append({
                "file": name,
                "locator": f"marc:{item}/{fname}:{off}:{ln}",
                "sha256": self._write_bytes(path, data),
                "fetched_at": self._now(),
            })

        self._write_bytes(
            os.path.join(target, "sources.json"),
            self._dumps({"isbn": isbn, "sources": sources}).encode("utf-8"),
        )
        self.log.info("Saved Open Library search data for book %s (%s)", book.id, isbn)

    # -- network (rate-limited, backed off) --------------------------------

    def _throttle(self):
        wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get_edition(self, isbn):
        delay = 1.0
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            try:
                return fetch_edition(isbn)
            except OLUnavailable:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(delay, MAX_BACKOFF))
                delay *= 2
        return None

    def _get_marc(self, item, fname, off, ln):
        delay = 1.0
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            try:
                return fetch_marc(item, fname, off, ln)
            except OLUnavailable:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(delay, MAX_BACKOFF))
                delay *= 2
        return None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _dumps(obj):
        return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def _write_bytes(path, data):
        with open(path, "wb") as f:
            f.write(data)
        return hashlib.sha256(data).hexdigest()