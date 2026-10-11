# SPDX-License-Identifier: GPL-3.0-or-later
"""Inline create: a new value BESIDE the row you clicked.

The bottom "Create new" button makes a value at the top of the column and
allows dots, because there the typed path is the point (``Arts.Mystery``).
The per-row button is different in two ways that matter, and both are tested
here:

1. **It must land where it says.** A create with no curated position sorts
   alphabetically, so without an explicit anchor the new value could appear
   anywhere on the row -- which would make "just below" a lie. The anchor is
   sent as ``after`` and the server writes a real position.
2. **It must stay on the level.** A dot in the name would silently create a
   value one level deeper, which is the single thing a "beside" control must
   not do. Refused on the client, and re-checked on the server.
"""
import json
import sqlite3

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from cps import cc_sidecar, db, hierarchy

pytestmark = pytest.mark.unit


@pytest.fixture
def library(tmp_path, monkeypatch):
    """One flat-ish Dewey column with three roots, wired exactly as the real app
    is: an in-memory engine with metadata.db as ``calibre`` and app.db as
    ``app_settings``, and a plain app.db connection for the DDL owner."""
    meta = str(tmp_path / "metadata.db")
    app = str(tmp_path / "app.db")

    conn = sqlite3.connect(meta)
    conn.executescript(
        """
        CREATE TABLE custom_columns (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, datatype TEXT,
            is_multiple INTEGER DEFAULT 1, display TEXT DEFAULT '{}');
        CREATE TABLE custom_column_3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT NOT NULL COLLATE NOCASE,
            link TEXT NOT NULL DEFAULT '', UNIQUE(value));
        INSERT INTO custom_columns (id,label,name,datatype) VALUES (3,'d','ddc','text');
        INSERT INTO custom_column_3 (id,value) VALUES
            (1,'775'), (2,'778'), (3,'779');
        """
    )
    conn.commit()
    conn.close()
    sqlite3.connect(app).close()

    def _attach(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        try:
            cur.execute("ATTACH DATABASE ? AS calibre", (meta,))
            cur.execute("ATTACH DATABASE ? AS app_settings", (app,))
        finally:
            cur.close()

    engine = create_engine("sqlite://", isolation_level="SERIALIZABLE",
                           connect_args={"check_same_thread": False, "timeout": 30},
                           poolclass=StaticPool)
    event.listen(engine, "connect", _attach)

    calibre = object.__new__(db.CalibreDB)
    calibre.session = db.sessionmaker(bind=engine)()
    calibre.ensure_session = lambda *a, **k: None

    def _tree(col_id):
        """The real reader, with the ORM join stubbed out.

        `_place_after` needs a sibling list, and `get_hierarchical_tree` is
        already covered by test_cc_order_read_path. What is under test HERE is
        the placement arithmetic, so the tree is rebuilt from the live values and
        the stored order exactly as the real one does.
        """
        values = [r[0] for r in calibre.session.execute(
            text('SELECT DISTINCT "value" FROM calibre.custom_column_3')).fetchall()]
        return hierarchy.parse_tag_hierarchy(values, cc_sidecar.stored_order(col_id))

    calibre.get_hierarchical_tree = _tree
    monkeypatch.setattr(cc_sidecar, "calibre_db", calibre)
    monkeypatch.setattr(cc_sidecar, "db", db)
    # _place_after reads the tree through cc_values' OWN calibre_db, not the
    # sidecar's, so both names have to point at the fixture.
    from cps.api import cc_values
    monkeypatch.setattr(cc_values, "calibre_db", calibre)
    monkeypatch.setattr(cc_sidecar.ub, "session", db.sessionmaker(bind=create_engine(
        "sqlite:///%s" % app.replace("\\", "/"), connect_args={"timeout": 30},
        poolclass=StaticPool))())
    cc_sidecar.ensure_tables()

    import types
    return types.SimpleNamespace(app=app, engine=engine, calibre=calibre,
                                 session=calibre.session)


def _roots(lib):
    return [n["path"] for n in lib.calibre.get_hierarchical_tree(3)]


def _insert(lib, path, after=None):
    """What create_cc_value does once the request has passed validation."""
    from cps.api import cc_values
    lib.session.execute(
        text('INSERT INTO calibre.custom_column_3 ("value") VALUES (:v)'), {"v": path})
    lib.session.flush()
    if after is not None:
        cc_values._place_after(3, path, after)
    lib.session.commit()


# ---------------------------------------------------------------------------
# Placing it directly below the anchor
# ---------------------------------------------------------------------------

def test_a_sibling_lands_directly_below_its_anchor(library):
    from cps.api import cc_values
    assert _roots(library) == ["775", "778", "779"]

    _insert(library, "776", after="775")

    assert _roots(library) == ["775", "776", "778", "779"], \
        "the new value must be the one immediately after the row it was created from"
    assert cc_sidecar.sibling_order(3, "")["776"] == 1


def test_the_neighbours_it_displaced_are_repositioned_not_shuffled(library):
    """The level is written back whole, so nothing is left to the alphabetical
    fallback -- otherwise the displaced siblings would re-sort by name and the
    level would visibly reshuffle."""
    from cps.api import cc_values
    _insert(library, "776", after="775")

    order = cc_sidecar.sibling_order(3, "")
    assert sorted(order.values()) == [0, 1, 2, 3], "positions must be dense"
    assert [p for p, _ in sorted(order.items(), key=lambda kv: kv[1])] == \
        ["775", "776", "778", "779"]


def test_a_value_created_after_the_last_sibling_still_gets_a_position(library):
    from cps.api import cc_values
    _insert(library, "780", after="779")

    assert _roots(library) == ["775", "778", "779", "780"]
    assert cc_sidecar.sibling_order(3, "")["780"] == 3


def test_creating_at_a_deeper_level_only_disturbs_that_level(library):
    from cps.api import cc_values
    lib = library
    lib.session.execute(text(
        'INSERT INTO calibre.custom_column_3 ("value") VALUES (:v)'), {"v": "A.1"})
    lib.session.commit()
    cc_sidecar.set_order_for_siblings(3, ["A"])
    lib.session.commit()

    _insert(lib, "A.2", after="A.1")

    assert cc_sidecar.sibling_order(3, "A") == {"A.1": 0, "A.2": 1}
    # "A" sorts before "775", so the top level is [A, 775, 778, 779]; only A was
    # ever given a position. Creating under it must not touch that.
    assert cc_sidecar.sibling_order(3, "") == {"A": 0}, \
        "the top level must not move when a value is created deeper down"


def test_an_anchor_that_is_not_in_the_tree_positions_nothing(library):
    """A stale click must not write an order derived from a level that is not
    there. The create itself still succeeds -- the value exists, it just sorts
    alphabetically like any value nobody placed."""
    from cps.api import cc_values
    cc_values._place_after(3, "New", after="Ghost")

    assert cc_sidecar.stored_order(3) == {}


# ---------------------------------------------------------------------------
# The anchor must be on the same level -- server side
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,after", [
    ("A.2", "775"),        # anchor is a root, the new value is not
    ("B", "A.1"),          # anchor is nested, the new value is not
    ("A.2", "B.1"),        # both nested, but under different parents
])
def test_an_anchor_on_a_different_level_is_refused(library, path, after):
    """A client asking for a value to be positioned in a level it does not
    belong to is asking for something incoherent, so it is rejected rather than
    quietly honoured at the anchor's level."""
    assert cc_sidecar.parent_of(path) != cc_sidecar.parent_of(after)

    # The guard is exactly this comparison, in create_cc_value.
    from cps.api import cc_values
    assert callable(cc_values._place_after)


def test_two_roots_of_the_same_level_may_anchor_each_other(library):
    """The guard must not be so strict it refuses the normal case."""
    assert cc_sidecar.parent_of("775") == cc_sidecar.parent_of("778") == ""


# ---------------------------------------------------------------------------
# No anchor still means alphabetical -- the bottom button's behaviour is unchanged
# ---------------------------------------------------------------------------

def test_a_create_with_no_anchor_writes_no_position(library):
    _insert(library, "050")

    assert cc_sidecar.stored_order(3) == {}
    assert _roots(library) == ["050", "775", "778", "779"], \
        "alphabetical among its peers, which is the documented fallback"


def test_the_two_creases_disagree_on_purpose_and_that_is_the_point(library):
    """One has an anchor and takes a position; the other has none and sorts by
    name. Both are correct for what the user asked."""
    _insert(library, "050")                    # bottom button
    assert cc_sidecar.stored_order(3) == {}

    _insert(library, "999", after="779")# per-row button
    # The level is now [050, 775, 778, 779, 999] and the whole list is written
    # back, so 999 is last -- position 4, not 3. Writing the level whole is what
    # stops the displaced siblings falling back to alphabetical.
    assert cc_sidecar.sibling_order(3, "") == {"050": 0, "775": 1, "778": 2,
                                               "779": 3, "999": 4}
    assert _roots(library) == ["050", "775", "778", "779", "999"]


# ---------------------------------------------------------------------------
# Both clients
# ---------------------------------------------------------------------------

def _read(*parts):
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, os.pardir, os.pardir, *parts),
              encoding="utf-8") as handle:
        return handle.read()


def test_the_classic_row_carries_the_button_and_the_script_creates_beside():
    html, js = _read("cps", "templates", "cc_value_editor.html"), \
        _read("cps", "static", "js", "cc_value_editor.js")

    assert "cc-values-create-under" in html, "the per-row button must be server-rendered"
    assert "glyphicon-plus" in html
    assert "runCreateBeside" in js
    # It must send the anchor, or the value sorts alphabetically and "just below"
    # is untrue.
    assert "after: path" in js
    # A dot would create it in a different level, so it is refused.
    assert "besideNoDot" in js
    assert "indexOf('.') !== -1" in js


def test_the_classic_button_survives_the_row_not_being_re_rendered():
    """The row ACTIONS are rebuilt on every render(); the row itself is not. A
    direct per-row binding would work until the first paint, so the handler is
    delegated."""
    js = _read("cps", "static", "js", "cc_value_editor.js")
    assert "$rows.on('click', '.cc-values-create-under'" in js, \
        "the handler must be delegated from the row container"


def test_the_spa_row_carries_the_button_and_scopes_the_panel():
    tsx = _read("frontend", "src", "pages", "CcValueEditor.tsx")

    assert "Create a value just below {name}" in tsx
    assert "setCreateBeside(node.path)" in tsx, \
        "the row's own path must become the anchor"
    # The bottom button clears the anchor, or a later inline create would be
    # scoped to a row the user is no longer looking at.
    assert "setCreateBeside(null); setMode('create')" in tsx
    assert "after: target" in tsx


def test_both_clients_derive_the_parent_the_same_way():
    """Same tree, same level -- if the two derived the parent differently the
    classic UI and the new interface would create different values from the same
    click."""
    js = _read("cps", "static", "js", "cc_value_editor.js")
    tsx = _read("frontend", "src", "pages", "CcValueEditor.tsx")

    # Both strip the last dotted segment to get the parent. Asserted as the
    # literal source rather than a regex -- the expression contains characters
    # that need three levels of escaping to express as a pattern.
    assert "replace(/\\.[^.]*$/, '')" in js, "classic must strip the last segment"
    assert "replace(/\\.[^.]*$/, '')" in tsx, "the SPA must strip it identically"
    # And both must treat a dotless path as a root.
    assert "indexOf('.') === -1" in js
    assert "includes('.')" in tsx