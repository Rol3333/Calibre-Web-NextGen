# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

# Open Library metadata provider.
#
# API docs: https://openlibrary.org/dev/docs/api/search
# - Search:  https://openlibrary.org/search.json?q=...&fields=...&limit=N
# - Covers:  https://covers.openlibrary.org/b/id/{cover_id}-L.jpg
#            https://covers.openlibrary.org/b/olid/{olid}-L.jpg
#
# Open Library is the Internet Archive's public book catalog. Free to query,
# no API key, generous rate limits (~100 req/min/IP). Returns ISBN, OLID,
# work key, edition keys, language, publisher, year, subjects.

import re
from typing import Dict, List, Optional
from urllib.parse import quote

from cps import logger
from cps.isoLanguages import get_language_name
from cps.services.isbn import isbn13 as normalize_isbn
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata
from cps.services.openlibrary_edition import (
    OLUnavailable,
    fetch_edition,
    fetch_marc,
    marc_locators,
    parse_marc,
    search_books,
    series_from_marc,
)


log = logger.create()

# "isbn:9781841761794" (also "ISBN 1-84176-179-6") — the form
# metadata_helper's SUPPORTS_ISBN_QUERY retry and the manual search box send.
_ISBN_Q = re.compile(r"^\s*isbn:\s*([0-9Xx\- ]+)\s*$")


class OpenLibrary(Metadata):
    __name__ = "Open Library"
    __id__ = "openlibrary"
    DESCRIPTION = "Open Library"
    META_URL = "https://openlibrary.org/"

    # search() resolves "isbn:<isbn>" through /isbn/<isbn>.json, so the
    # auto-fetch may retry this provider by the book's ISBN when the title
    # search misses (same contract as databazeknih.py).
    SUPPORTS_ISBN_QUERY = True
    # The edition record (and the MARC series fields) carry series + position,
    # so this provider can fill a series the winning provider left empty.
    FILLS_SERIES = True

    BOOK_URL_TEMPLATE = "https://openlibrary.org{key}"  # key starts with /works/...
    COVER_BY_ID_URL = "https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
    COVER_BY_OLID_URL = "https://covers.openlibrary.org/b/olid/{olid}-L.jpg"

    SEARCH_FIELDS = ",".join([
        "key", "title", "author_name", "first_publish_year",
        "language", "isbn", "publisher", "cover_i", "cover_edition_key",
        "edition_key", "number_of_pages_median", "subject",
    ])

    SEARCH_LIMIT = 10
    # Foreground MARC enrichment is bounded: a search dialog must not wait on
    # Archive.org. The full set of locators is retrieved by the save task.
    MARC_MAX_LOCATORS = 1

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[List[MetaRecord]]:
        if not self.active:
            return []

        # An ISBN query goes straight to the edition endpoint: one request, the
        # exact edition, and no title-tokenization guesswork.
        if m := _ISBN_Q.match(query or ""):
            isbn = normalize_isbn(m.group(1))
            if isbn:
                return self._search_by_isbn(isbn, generic_cover, locale)

        title_tokens = list(self.get_title_tokens(query, strip_joiners=False))
        if not title_tokens:
            return []
        q = "+".join(quote(t.encode("utf-8")) for t in title_tokens)

        # The search request goes through the shared Open Library HTTP service:
        # same rate limiter, explicit timeouts, bounded retries with backoff and
        # Retry-After handling. A failure is logged and reported as an empty
        # result, but the failure information is preserved in the log (fixes_v2
        # P1) rather than being indistinguishable from "no results".
        try:
            payload = search_books(q, OpenLibrary.SEARCH_FIELDS, OpenLibrary.SEARCH_LIMIT)
        except OLUnavailable as exc:
            log.warning("OpenLibrary search failed: %s", exc)
            return []
        except ValueError as exc:
            log.warning("OpenLibrary search returned invalid data: %s", exc)
            return []

        results: List[MetaRecord] = []
        for doc in payload.get("docs", []) or []:
            record = self._parse_doc(doc, generic_cover=generic_cover, locale=locale)
            if record is not None:
                results.append(record)
        return results

    def _search_by_isbn(
        self, isbn: str, generic_cover: str, locale: str
    ) -> List[MetaRecord]:
        """Resolve an ISBN to its Open Library edition record.

        ``isbn`` is already an ISBN-13 (from ``cps.services.isbn.isbn13``),
        which is the form ``/isbn/<isbn>.json`` accepts for both ISBN-10 and
        ISBN-13 editions.
        """
        try:
            edition = fetch_edition(isbn)
        except OLUnavailable as exc:
            log.warning("OpenLibrary ISBN lookup failed for %s: %s", isbn, exc)
            return []
        if not edition:
            return []
        record = self._record_from_edition(edition, generic_cover, locale)
        return [record] if record else []

    def _record_from_edition(
        self, edition: Dict, generic_cover: str, locale: str
    ) -> Optional[MetaRecord]:
        """Build a MetaRecord from an /isbn/<isbn>.json edition object."""
        title = edition.get("title")
        if not title:
            return None
        work_key = self._work_key(edition)
        record = MetaRecord(
            id=work_key or edition.get("key") or title,
            title=title,
            authors=self._authors(edition),
            url=self._book_url(edition, work_key),
            source=MetaSourceInfo(
                id=self.__id__,
                description=OpenLibrary.DESCRIPTION,
                link=OpenLibrary.META_URL,
            ),
        )
        record.cover = self._edition_cover_url(edition, generic_cover)
        record.publisher = self._first_or_none(edition.get("publishers"))
        record.publishedDate = self._publish_date(edition.get("publish_date"))
        record.languages = self._edition_languages(edition, locale)
        record.tags = self._truncate_subjects(edition.get("subjects"))
        record.identifiers = self._edition_identifiers(edition, work_key)
        record.series, record.series_index = self._series(edition)
        return record

    @staticmethod
    def _work_key(edition: Dict) -> str:
        works = edition.get("works") or []
        if works and isinstance(works[0], dict):
            return works[0].get("key") or ""
        return ""

    @staticmethod
    def _book_url(edition: Dict, work_key: str) -> str:
        if work_key:
            return OpenLibrary.BOOK_URL_TEMPLATE.format(key=work_key)
        key = edition.get("key")
        if key:
            return OpenLibrary.BOOK_URL_TEMPLATE.format(key=key)
        return OpenLibrary.META_URL

    @staticmethod
    def _authors(edition: Dict) -> List[str]:
        """Author names from ``by_statement``, conservatively.

        ``by_statement`` is a bibliographic statement, not a structured author
        list: it can lead with "by", and can name editors, illustrators or
        translators. Only a plain statement is used, and a leading "by" is
        stripped. Anything that looks like a contributor list (semicolons,
        bracketed roles, "ed."/"ill."/"trans.") is left to the search endpoint,
        which returns proper author names.
        """
        by_statement = (edition.get("by_statement") or "").strip()
        if not by_statement:
            return []
        # A structured statement with several contributors is not safe to split.
        if ";" in by_statement or re.search(r"[\[\]]|\b(ed|eds|editor|editors|ill|illustrator|"
                                            r"trans|translator|tr)\b\.?", by_statement, re.I):
            return []
        name = re.sub(r"^\s*by\s+", "", by_statement, flags=re.I).strip(" .;,")
        return [name] if name else []

    @staticmethod
    def _edition_cover_url(edition: Dict, generic_cover: str) -> str:
        covers = edition.get("covers") or []
        if covers:
            return OpenLibrary.COVER_BY_ID_URL.format(cover_id=covers[0])
        key = edition.get("key")
        if key:
            olid = key.rsplit("/", 1)[-1]
            if olid:
                return OpenLibrary.COVER_BY_OLID_URL.format(olid=olid)
        return generic_cover

    @staticmethod
    def _publish_date(value) -> str:
        if not value:
            return ""
        text = str(value).strip()
        m = re.search(r"\b(1[4-9]\d\d|20\d\d)\b", text)
        return m.group(1) if m else text

    @staticmethod
    def _edition_languages(edition: Dict, locale: str) -> List[str]:
        languages: List[str] = []
        for entry in edition.get("languages") or []:
            code = entry.get("key", "").rsplit("/", 1)[-1] if isinstance(entry, dict) else ""
            if not code:
                continue
            try:
                name = get_language_name(locale, code)
            except Exception:
                name = None
            if name and name not in languages:
                languages.append(name)
        return languages

    @staticmethod
    def _edition_identifiers(edition: Dict, work_key: str) -> Dict[str, str]:
        identifiers: Dict[str, str] = {}
        isbn10 = OpenLibrary._first_or_none(edition.get("isbn_10"))
        isbn13 = OpenLibrary._first_or_none(edition.get("isbn_13"))
        # Prefer the ISBN-13; fall back to ISBN-10.
        isbn = isbn13 or isbn10
        if isbn:
            identifiers["isbn"] = isbn
        if work_key.startswith("/works/"):
            identifiers["openlibrary"] = work_key.split("/", 2)[-1]
        lccn = edition.get("lccn")
        if isinstance(lccn, list):
            lccn = lccn[0] if lccn else None
        if lccn:
            identifiers["lccn"] = str(lccn)
        ocaid = edition.get("ocaid")
        if ocaid:
            identifiers["internetarchive"] = ocaid
        return identifiers

    @staticmethod
    def _series(edition: Dict) -> tuple:
        """Series name/index from the edition's MARC 830/490 fields.

        The search endpoint has no series, but the edition's ``source_records``
        point at the MARC records the catalog was built from, and those carry
        the authoritative series statement. Foreground lookup is bounded to
        ``MARC_MAX_LOCATORS`` so a slow Archive.org cannot stall the dialog; the
        save task retrieves every locator. Expected per-source failures (a
        MARC-8 record, a download error, malformed data) are logged at debug
        and skipped so a later valid locator can still supply the series.
        """
        for item, fname, off, ln in marc_locators(edition)[:OpenLibrary.MARC_MAX_LOCATORS]:
            try:
                data = fetch_marc(item, fname, off, ln)
                name, index = series_from_marc(parse_marc(data))
                if name:
                    return name, index if index is not None else 0
            except (OLUnavailable, ValueError) as exc:
                log.debug("OpenLibrary: skipping MARC source %s/%s@%s: %s",
                          item, fname, off, exc)
                continue
        return None, 0

    def _parse_doc(
        self, doc: Dict, generic_cover: str, locale: str
    ) -> Optional[MetaRecord]:
        title = doc.get("title")
        work_key = doc.get("key")
        if not title or not work_key:
            return None

        record = MetaRecord(
            id=work_key,
            title=title,
            authors=list(doc.get("author_name") or []),
            url=OpenLibrary.BOOK_URL_TEMPLATE.format(key=work_key),
            source=MetaSourceInfo(
                id=self.__id__,
                description=OpenLibrary.DESCRIPTION,
                link=OpenLibrary.META_URL,
            ),
        )
        record.cover = self._cover_url(doc, generic_cover=generic_cover)
        record.publisher = self._first_or_none(doc.get("publisher"))
        year = doc.get("first_publish_year")
        record.publishedDate = str(year) if year else ""
        record.languages = self._parse_languages(doc, locale=locale)
        record.tags = self._truncate_subjects(doc.get("subject"))
        record.identifiers = self._parse_identifiers(doc)
        return record

    @staticmethod
    def _cover_url(doc: Dict, generic_cover: str) -> str:
        cover_id = doc.get("cover_i")
        if cover_id:
            return OpenLibrary.COVER_BY_ID_URL.format(cover_id=cover_id)
        olid = doc.get("cover_edition_key")
        if olid:
            return OpenLibrary.COVER_BY_OLID_URL.format(olid=olid)
        return generic_cover

    @staticmethod
    def _first_or_none(value):
        if isinstance(value, list) and value:
            return value[0]
        return value or None

    @staticmethod
    def _parse_languages(doc: Dict, locale: str) -> List[str]:
        codes = doc.get("language") or []
        languages: List[str] = []
        for code3 in codes[:5]:
            try:
                name = get_language_name(locale, code3)
            except Exception:
                name = None
            if name and name not in languages:
                languages.append(name)
        return languages

    @staticmethod
    def _truncate_subjects(subjects) -> List[str]:
        if not subjects:
            return []
        return list(subjects[:8])

    @staticmethod
    def _parse_identifiers(doc: Dict) -> Dict[str, str]:
        identifiers: Dict[str, str] = {}
        # Pick a representative ISBN-13 first, fall back to ISBN-10.
        isbn = OpenLibrary._first_or_none(doc.get("isbn"))
        if isbn:
            identifiers["isbn"] = isbn
        # OLID (work key trimmed of the /works/ prefix) gives the Open Library
        # work URL in calibre conventions.
        work_key = doc.get("key") or ""
        if work_key.startswith("/works/"):
            identifiers["openlibrary"] = work_key.split("/", 2)[-1]
        return identifiers
