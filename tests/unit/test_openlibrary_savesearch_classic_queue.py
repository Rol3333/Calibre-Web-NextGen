# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""The classic edit page must tell /metadata/search which book it is for.

The Open Library archive is queued by the search endpoint only when it knows
the book id. The classic modal posted only ``query``, so the queue never ran
and ``data/openlibrary/`` was never written (fixes_v3).

``cps/search_metadata.py`` cannot be imported in the unit env, so the helper is
extracted from source and exercised directly, as in
``test_metadata_search_hierarchy_order_405.py``. The wiring points are pinned
by source inspection.
"""
from __future__ import annotations

import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))


def _src(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _load_func(rel_path, name):
    tree = ast.parse(_src(rel_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace = {"re": re}
            exec(compile(module, rel_path, "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"{name} not found in {rel_path}")


def test_book_id_from_referrer_recognises_the_classic_edit_page():
    func = _load_func("cps/search_metadata.py", "_book_id_from_referrer")
    assert func("http://localhost:8083/admin/book/2898") == "2898"
    assert func("http://localhost:8083/admin/book/2898/") == "2898"
    assert func("http://localhost:8083/admin/book/2898?x=1") == "2898"
    assert func("http://localhost:8083/admin/book/2898#top") == "2898"


def test_book_id_from_referrer_ignores_other_pages():
    func = _load_func("cps/search_metadata.py", "_book_id_from_referrer")
    assert func("http://localhost:8083/admin/") is None
    assert func("http://localhost:8083/admin/book/abc") is None
    assert func("") is None
    assert func(None) is None


def test_search_route_queues_the_save_for_the_referred_book():
    source = _src("cps/search_metadata.py")
    assert "queue_save_search_data(int(book_id))" in source
    assert "_book_id_from_referrer(request.referrer)" in source


def test_classic_modal_sends_book_id():
    js = _src("cps/static/js/get_meta.js")
    assert 'book_id: $("#meta-search input[name=\'book_id\']").val()' in js
    template = _src("cps/templates/book_edit.html")
    assert '<input type="hidden" name="book_id" value="{{ book.id }}">' in template


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"PASS {name}")
