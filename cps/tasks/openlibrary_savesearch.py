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

This is *not* the library backup workflow (``TaskBackupMetadata``, which
writes ``metadata.opf``). It only records what Open Library returned for the
book, so the lookup can be reproduced later without re-querying.

Layout written under ``<book_path>/<book.path>/data/openlibrary/``:

* ``edition.json``  — the raw Open Library edition object.
* ``marc_<n>_<item>_<offset>.mrc`` — one file per MARC record, ``<n>`` is the
  record's position (1-based), ``<item>``/``<offset>`` come from the
  ``source_records`` locator with the ``:``/``/`` stripped so the name is
  filesystem-safe.
* ``sources.json`` — the manifest and the archive's completion record: one
  entry per source with its ``locator``, ``file``, SHA-256, fetch date and, for
  a source that could not be retrieved, its ``error``.

Completion is driven by the manifest, never by the mere presence of
``edition.json``: a run that saved the edition but failed a MARC download is
``partial`` and is retried on the next run, while ``complete``/``no_marc`` are
left alone. Files are written atomically (temp file + ``os.replace``) and the
manifest is written last, so an interrupted run cannot leave a file that looks
complete. A per-book lock keeps two tasks queued close together from racing.
"""

import hashlib
import json
import os
import re
import threading
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
from cps.services.worker import CalibreTask, STAT_CANCELLED, STAT_ENDED
from flask_babel import lazy_gettext as N_

log = logger.create()

# Cap on a single back-off wait, and on the attempts per request.
MAX_BACKOFF = 8
MAX_ATTEMPTS = 4

# Manifest status values.
STATUS_COMPLETE = "complete"    # edition + every discovered MARC source saved
STATUS_PARTIAL = "partial"      # edition saved, one or more MARC sources failed
STATUS_NO_MARC = "no_marc"      # edition saved, no MARC locators were present
STATUS_FAILED = "failed"        # edition retrieval or the essential write failed

# One lock per book path, so two tasks for the same book cannot interleave.
_BOOK_LOCKS = {}
_BOOK_LOCKS_GUARD = threading.Lock()


def _book_lock(key):
    with _BOOK_LOCKS_GUARD:
        lock = _BOOK_LOCKS.get(key)
        if lock is None:
            lock = _BOOK_LOCKS[key] = threading.Lock()
        return lock


def _safe(value: str) -> str:
    """Strip the characters that are illegal or awkward in a file name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "")


def _book_isbn(book) -> str:
    """The book's first checksum-valid ISBN-13, or ''."""
    for identifier in getattr(book, "identifiers", []) or []:
        itype = (getattr(identifier, "type", "") or "").lower()
        if itype in ("isbn", "isbn13", "isbn_13", "isbn10", "isbn_10"):
            isbn = normalize_isbn(getattr(identifier, "val", ""))
            if isbn:
                return isbn
    return ""


class TaskSaveOpenLibrarySearch(CalibreTask):
    """Write the Open Library edition + MARC records into the book folder."""

    def __init__(self, book_id=None, task_message=N_("Saving Open Library search data")):
        super().__init__(task_message)
        self.book_id = book_id
        self.log = logger.create()
        self.calibre_db = None

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

    def _cancelled(self):
        return self.stat in (STAT_CANCELLED, STAT_ENDED)

    def _save_all(self):
        books = self.calibre_db.session.query(db.Books).all()
        count = len(books) or 1
        for i, book in enumerate(books, start=1):
            if self._cancelled():
                return
            self._save_book(book)
            self.progress = i / count

    def _save_book(self, book):
        if book is None or self._cancelled():
            return
        isbn = _book_isbn(book)
        if not isbn:
            self.log.debug("Book %s has no valid ISBN; skipping Open Library save", getattr(book, "id", "?"))
            return

        # Google Drive storage has no sidecar-directory equivalent here; say so
        # rather than writing to an unrelated local path.
        if getattr(config, "config_use_google_drive", False):
            self.log.warning("Open Library save is not supported with Google Drive storage; "
                             "skipping book %s", getattr(book, "id", "?"))
            return

        target = os.path.join(config.get_book_path(), book.path, "data", "openlibrary")
        with _book_lock(book.path):
            self._save_book_locked(book, isbn, target)

    def _save_book_locked(self, book, isbn, target):
        # Create the directory before any manifest write, including the failure
        # manifest written when the edition API returns 404 or a network error.
        os.makedirs(target, exist_ok=True)

        manifest_path = os.path.join(target, "sources.json")
        manifest = self._read_manifest(manifest_path)

        # Only a complete archive for the SAME ISBN is skippable. A partial
        # archive (or one recorded under a different ISBN) is never skipped.
        if (manifest and manifest.get("isbn") == isbn
                and manifest.get("status") in (STATUS_COMPLETE, STATUS_NO_MARC)):
            self.log.debug("Book %s already has complete Open Library data for %s; skipping",
                           book.id, isbn)
            return

        # Trust only files that are still on disk and whose SHA-256 matches the
        # manifest. Failed locators from a previous partial run are kept so they
        # are retried, and so a partial archive is never demoted to no_marc by
        # losing them.
        prev = list(manifest.get("sources", [])) if manifest else []
        verified = {}  # locator -> source entry (edition or verified MARC file)
        for s in prev:
            if (s.get("locator") and s.get("sha256") and not s.get("error")
                    and self._verify_file(target, s.get("file"), s["sha256"])):
                verified[s["locator"]] = s
        failed_locators = [s.get("locator") for s in prev
                           if s.get("error") and s.get("locator")]
        edition_key = manifest.get("edition_key") if manifest else None

        edition_path = os.path.join(target, "edition.json")
        edition = None
        if f"isbn:{isbn}" not in verified:
            if self._cancelled():
                return
            try:
                edition = self._get_edition(isbn)
            except OLUnavailable as exc:
                self.log.warning("Open Library unavailable for %s: %s", isbn, exc)
                self._write_manifest(manifest_path, isbn, STATUS_FAILED,
                                     self._final_sources(verified, failed_locators), None)
                return
            if not edition:
                self.log.info("No Open Library edition for ISBN %s (book %s)", isbn, book.id)
                self._write_manifest(manifest_path, isbn, STATUS_FAILED,
                                     self._final_sources(verified, failed_locators), None)
                return
            digest = self._atomic_write(edition_path, self._dumps(edition).encode("utf-8"))
            verified[f"isbn:{isbn}"] = {
                "file": "edition.json", "locator": f"isbn:{isbn}",
                "sha256": digest, "fetched_at": self._now(),
            }
            edition_key = self._edition_key(edition)
        else:
            # Load the saved edition to reconstruct the complete set of
            # source_records, so locators missing from the manifest still get
            # another chance.
            edition = self._load_edition(edition_path)

        # Reconstruct the full locator set: from the loaded edition when
        # available, otherwise from verified + failed manifest entries.
        locators = marc_locators(edition) if edition else self._locators_from(
            list(verified.values()) + [{"locator": loc} for loc in failed_locators])
        failed = []
        for n, (item, fname, off, ln) in enumerate(locators, start=1):
            if self._cancelled():
                self._write_manifest(manifest_path, isbn, STATUS_PARTIAL,
                                     self._final_sources(verified, failed), edition_key)
                return
            locator = f"marc:{item}/{fname}:{off}:{ln}"
            if locator in verified:
                continue  # already verified on disk; do not re-download
            name = f"marc_{n}_{_safe(item)}_{_safe(off)}.mrc"
            try:
                data = self._get_marc(item, fname, off, ln)
                digest = self._atomic_write(os.path.join(target, name), data)
                verified[locator] = {
                    "file": name, "locator": locator, "sha256": digest,
                    "fetched_at": self._now(),
                }
            except (OLUnavailable, ValueError) as exc:
                self.log.warning("MARC %s/%s@%s unavailable: %s", item, fname, off, exc)
                failed.append({"locator": locator, "error": str(exc)})

        if self._cancelled():
            self._write_manifest(manifest_path, isbn, STATUS_PARTIAL,
                                 self._final_sources(verified, failed), edition_key)
            return

        if not locators and not failed:
            status = STATUS_NO_MARC
        elif failed:
            status = STATUS_PARTIAL
        else:
            status = STATUS_COMPLETE
        self._write_manifest(manifest_path, isbn, status,
                             self._final_sources(verified, failed), edition_key)
        self.log.info("Saved Open Library search data for book %s (%s): %s",
                      book.id, isbn, status)

    # -- network (rate-limited, backed off) --------------------------------

    def _get_edition(self, isbn):
        delay = 1.0
        for attempt in range(MAX_ATTEMPTS):
            if self._cancelled():
                raise OLUnavailable("cancelled")
            try:
                return fetch_edition(isbn)
            except OLUnavailable:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                self._backoff(delay)
                delay *= 2
        return None

    def _get_marc(self, item, fname, off, ln):
        delay = 1.0
        for attempt in range(MAX_ATTEMPTS):
            if self._cancelled():
                raise OLUnavailable("cancelled")
            try:
                return fetch_marc(item, fname, off, ln)
            except OLUnavailable:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                self._backoff(delay)
                delay *= 2
        return None

    def _backoff(self, delay):
        """Sleep in small slices so a cancel is noticed promptly."""
        end = time.monotonic() + min(delay, MAX_BACKOFF)
        while time.monotonic() < end:
            if self._cancelled():
                return
            time.sleep(min(0.2, end - time.monotonic()))

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _read_manifest(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _locators_from(sources):
        """Recover locators from a manifest when the edition JSON is gone."""
        out = []
        for s in sources:
            loc = s.get("locator", "")
            m = re.match(r"^marc:([^/]+)/([^:]+):(\d+):(\d+)$", loc)
            if m:
                out.append(m.groups())
        return out

    @staticmethod
    def _edition_key(edition):
        key = edition.get("key")
        if key:
            return key
        works = edition.get("works") or []
        if works and isinstance(works[0], dict):
            return works[0].get("key")
        return None

    @staticmethod
    def _load_edition(path):
        """Load a previously saved edition.json, or None."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _verify_file(target, filename, sha256):
        """True only when ``filename`` exists and its SHA-256 matches."""
        if not filename or not sha256:
            return False
        try:
            with open(os.path.join(target, filename), "rb") as f:
                data = f.read()
            return hashlib.sha256(data).hexdigest() == sha256
        except OSError:
            return False

    @staticmethod
    def _final_sources(verified, failed):
        """Manifest source list: verified entries plus failed locators."""
        return list(verified.values()) + list(failed)

    def _write_manifest(self, path, isbn, status, sources, edition_key):
        """Write the manifest best-effort.

        A failure to write the manifest must not obscure the original network
        error that led here, so it is logged and swallowed.
        """
        payload = {
            "isbn": isbn,
            "edition_key": edition_key,
            "status": status,
            "sources": sources,
            "updated_at": self._now(),
        }
        try:
            self._atomic_write(path, self._dumps(payload).encode("utf-8"))
        except OSError as exc:
            self.log.warning("Failed to write Open Library manifest %s: %s", path, exc)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _dumps(obj):
        return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def _atomic_write(path, data):
        """Write ``data`` to ``path`` atomically; return its SHA-256."""
        tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return hashlib.sha256(data).hexdigest()