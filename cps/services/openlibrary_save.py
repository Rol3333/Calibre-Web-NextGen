# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Single entry point for queueing the Open Library search-data save.

Both the ingest auto-fetch (``cps.metadata_helper``) and the manual metadata
apply (``cps.editbooks.edit_book_param``) call :func:`queue_save_search_data`,
so the two call sites cannot drift. Queueing is best-effort: a failure here
must never undo a metadata apply that has already committed.
"""

from cps import logger

log = logger.create()


def queue_save_search_data(book_id):
    """Queue the Open Library search-data save for ``book_id``.

    The save is keyed on the book's own ISBN, so it runs whichever provider
    supplied the metadata. Never raises.
    """
    if book_id is None:
        return
    try:
        from cps.services.worker import WorkerThread
        from cps.tasks.openlibrary_savesearch import TaskSaveOpenLibrarySearch
        WorkerThread.add(None, TaskSaveOpenLibrarySearch(book_id=book_id))
        log.info("Queued Open Library search-data save for book %s", book_id)
    except Exception as e:  # noqa: BLE001 — saving is best-effort
        log.warning("Could not queue Open Library search-data save for book %s: %s",
                    book_id, e)