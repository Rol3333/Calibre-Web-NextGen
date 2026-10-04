# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""Editing the VALUES of a custom column, for the SPA browse view.

Where ``cps/api/columns.py`` is the read-only twin of the classic
``/custom_column/<id>`` views, this is the write side of the same tree:

    POST /api/v1/columns/<id>/values/rename
    POST /api/v1/columns/<id>/values/merge
    POST /api/v1/columns/<id>/values/delete
    POST /api/v1/columns/<id>/values/move
    POST /api/v1/columns/<id>/values/create

Design notes that matter more than the code below:

**The hierarchy is not stored.** ``Arts.Fiction`` is an independent row from
``Arts``; ``hierarchy.parse_tag_hierarchy`` splits on ``.`` at read time. So
renaming a node is *not* one UPDATE -- it rewrites every stored value beneath
it, on every book carrying one. That is why rename cascades and why Move is
just "rename with a different parent".

**One value is shared by many books.** ``custom_column_<id>`` holds a single
row per distinct value, linked to every book that carries it, so one rename
moves all of them at once -- exactly as renaming a Calibre tag does.

**``value`` is UNIQUE.** Renaming onto an existing value is therefore a merge,
not a conflict that can be refused. That is Calibre's own behaviour, and it is
mirrored here, but merges are never applied silently: the response carries the
merge list and the caller must echo ``confirm_merges: true``, so a typo cannot
consume a neighbouring value. This is the same opt-in contract as
``cps/api/browse.py::rename_tag`` (#973).

**Merge and Delete are leaves-only.** Folding two interior nodes together, or
deleting a node that has children, are both ambiguous because the children are
independent rows; the caller is told to use Rename or Move instead. On a flat
column every value is a leaf, so the restriction costs nothing.

**Locking.** Every mutation runs inside ``metadata_db_write_lock()`` and
re-checks uniqueness there, so two workers cannot both pass the check before
either commits. Book-side bookkeeping (``mark_book_modified``) happens in the
same transaction; ``log_metadata_change`` runs only after it commits.
"""
from flask import jsonify, request
from flask_babel import gettext as _
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import api_v1
from .browse import _require_metadata_editor
from .columns import _get_column_or_none, _may_browse_columns, _not_found
from .. import calibre_db, db, hierarchy, helper, logger
from ..services.calibre_db_lock import metadata_db_write_lock
from ..usermanagement import login_required_if_no_ano

log = logger.create()


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _relation(col_id):
    """The ``Books.custom_column_<id>`` collection for this column."""
    return getattr(db.Books, 'custom_column_' + str(col_id))


def _invalidate_hierarchy_cache():
    """Drop the cached hierarchical/flat verdict for every column.

    ``get_hierarchical_column_ids`` caches process-wide for 300s and
    ``is_flat_cc_column`` reads it. A rename or a move can create or destroy a
    prefix relationship, so without this the browse tree, the sidebar and the
    edit picker can disagree with the data for up to five minutes.
    """
    for attr in ('_hier_cache', '_legacy_hier_cache'):
        if hasattr(calibre_db, attr):
            setattr(calibre_db, attr, None)


def _value_row(col_id, value):
    """The row holding exactly ``value``, or None."""
    cc = db.cc_classes.get(col_id)
    if cc is None:
        return None
    return calibre_db.session.query(cc).filter(cc.value == value).one_or_none()


def _raw_values_under(col_id, path, hierarchical):
    """Every stored spelling the node at ``path`` counts, descendants included.

    A node can be spelled several ways in the database (``'Arts.'`` and
    ``'Arts. DB'`` both normalise to ``Arts``), so an operation must move all
    the spellings, not just the one the UI displayed.
    """
    if not path:
        return set()
    if not hierarchical:
        return {path}
    node = hierarchy.get_node_by_path(calibre_db.get_hierarchical_tree(col_id), path)
    if node is None:
        return set()
    return set(hierarchy.subtree_values(node))


def _leaves_of(tree):
    """``(exists, has_children)`` predicates for one tree, for the rule module.

    :func:`hierarchy.plan_merge` and :func:`hierarchy.plan_delete` take these
    as arguments precisely so the rules do not need to know how a tree is
    fetched -- which is what lets the same rules run in the classic UI.
    """
    def exists(path):
        return hierarchy.get_node_by_path(tree, path) is not None

    def has_children(path):
        node = hierarchy.get_node_by_path(tree, path)
        return bool(node and node['children'])

    return exists, has_children


# The rules themselves live in cps/hierarchy.py, where they are pure and
# reachable from the classic Jinja UI as well as from this API. These two
# functions are the only translation shims left: they gather the value set from
# the database and turn the rule module's error CODES into translatable
# sentences.

def _explain(error, context=''):
    """Render a ``(code, subject)`` refusal from ``hierarchy`` as a message.

    Keeping the strings here rather than in the rules is what lets
    ``hierarchy`` stay free of flask_babel, and it is the single place that
    decides what each refusal reads like.
    """
    code, subject = error
    if code == hierarchy.ERR_EMPTY_NAME:
        return _('Name cannot be empty')
    if code == hierarchy.ERR_EMPTY_VALUE:
        return _('Value cannot be empty')
    if code == hierarchy.ERR_INTO_ITSELF:
        return (_('A node cannot be moved inside itself') if context == 'rename'
                else _('A value cannot be moved inside itself'))
    if code == hierarchy.ERR_ALREADY_THERE:
        return _('That value is already there')
    if code == hierarchy.ERR_MISSING_SOURCE:
        return _('One of the selected values no longer exists')
    if code == hierarchy.ERR_SOURCE_HAS_CHILDREN:
        return (_('“{name}” has sub-values. Delete those instead.')
                .format(name=subject) if context == 'delete'
                else _('“{name}” has sub-values. Move or rename it instead of merging.')
                .format(name=subject))
    if code == hierarchy.ERR_MERGE_INTO_ITSELF:
        return _('A value cannot be merged into itself')
    if code == hierarchy.ERR_MERGE_INTO_DESCENDANT:
        return _('A value cannot be merged into one of its sub-values')
    return _('That value cannot be changed')


def _plan_rename(col_id, path, new_name, hierarchical):
    """Map every stored value under ``path`` to its new spelling.

    Returns ``(mapping, errors)``. The mapping itself is built by
    ``hierarchy.plan_rename``; this only supplies the value set and the words.
    """
    mapping, error = hierarchy.plan_rename(
        path, new_name, _raw_values_under(col_id, path, hierarchical), hierarchical)
    return mapping, [_explain(error, 'rename')] if error else []


def _plan_move(col_id, path, new_parent):
    """Map every stored value under ``path`` to its relocated spelling.

    ``{"path": "Adobe", "new_parent": "Software"}`` yields ``Adobe ->
    Software.Adobe``, ``Adobe.Photoshop -> Software.Adobe.Photoshop`` and so on.
    """
    path = hierarchy.join_path([path])
    mapping, error, new_path = hierarchy.plan_move(
        path, new_parent, _raw_values_under(col_id, path, True))
    return mapping, [_explain(error)] if error else [], new_path


def _resolve_merges(col_id, mapping):
    """Split ``mapping`` into plain renames and merges.

    A target that already exists is a merge (the value column is UNIQUE), and
    merges are only ever applied with the caller's explicit consent.
    """
    renames, merges = {}, []
    for old, new in mapping.items():
        if old == new:
            continue
        existing = _value_row(col_id, new)
        if existing is None:
            renames[old] = new
            continue
        row = _value_row(col_id, old)
        merges.append({
            'from': old,
            'into': new,
            'books': len(row.books) if row is not None else 0,
        })
    return renames, merges


def _apply_merge(col_id, from_value, into_row):
    """Move every book off ``from_value`` onto ``into_row``, then drop the row.

    Both column shapes expose a list-like ``Books.custom_column_<id>``
    collection -- the multi-value case goes through the link table, the
    single-value case through its own ``book`` column -- so append/remove works
    for either. A book already carrying the survivor must end up with one
    entry, not two.
    """
    rel = _relation(col_id)
    row = _value_row(col_id, from_value)
    if row is None:
        return []
    affected = list(row.books)
    for book in affected:
        collection = getattr(book, rel.key)
        if into_row not in collection:
            collection.append(into_row)
        collection.remove(row)
        helper.mark_book_modified(book, set_dirty=True)
    if not row.books:
        calibre_db.session.delete(row)
    return affected


def _commit_and_log(affected):
    """Commit, then log the change per book.

    The audit trail is only written after the transaction succeeded: a logged
    change that was rolled back would misreport what is on disk.
    """
    for book in affected:
        helper.log_metadata_change(book, {"custom_columns": ", ".join(
            "%s=%s" % (col, val) for col, val in _column_summary(book).items())})
    return len(affected)


def _column_summary(book):
    """Every custom-column value on a book, for the change log."""
    summary = {}
    for attr, value in book.__dict__.items():
        if not attr.startswith('custom_column_'):
            continue
        rows = value or []
        summary[attr] = ", ".join(str(r.value) for r in rows if getattr(r, 'value', None) is not None)
    return summary


def _bad_request(message, code='invalid_request'):
    return jsonify({"error": {"code": code, "message": message}}), 400


def _conflict(message, conflicts):
    return jsonify({"error": {
        "code": "conflict", "message": message, "conflicts": conflicts}}), 409


def _column_or_404(col_id):
    """Resolve the column, honouring the per-user visibility toggle."""
    if not _may_browse_columns():
        return None
    col = _get_column_or_none(col_id)
    if col is None or col_id not in db.cc_classes:
        return None
    return col


# --------------------------------------------------------------------------- #
# Rename
# --------------------------------------------------------------------------- #

@api_v1.route("/columns/<int:col_id>/values/rename", methods=["POST"])
@login_required_if_no_ano
def rename_cc_value(col_id):
    """Rename one node, cascading to every value beneath it.

    ``{"path": "Arts", "new_name": "Culture", "confirm_merges": true}``

    Because the value column is UNIQUE, renaming onto an existing value is a
    merge. That is never applied on first contact: the response is a 409
    carrying the exact list of merges, and the caller repeats the request with
    ``confirm_merges`` to go ahead. This mirrors ``rename_tag`` (#973) -- there
    is no undo, so a typo that happens to match a neighbouring value must not
    silently consume it.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    col = _column_or_404(col_id)
    if col is None:
        return _not_found("Column not found")

    payload = request.get_json(silent=True) or {}
    path = payload.get("path")
    if not isinstance(path, str) or not path.strip():
        return _bad_request(_('Pick a node to rename'))
    if "," in path:
        return _bad_request(_('A node name cannot contain a comma'))
    new_name = payload.get("new_name")
    if not isinstance(new_name, str) or not new_name.strip():
        return _bad_request(_('Name cannot be empty'))
    if "," in new_name:
        return _bad_request(_('A node name cannot contain a comma'))

    path = hierarchy.join_path([path]) if not calibre_db.is_flat_cc_column(col_id) else path
    hierarchical = not calibre_db.is_flat_cc_column(col_id)
    mapping, errors = _plan_rename(col_id, path, new_name, hierarchical)
    if errors:
        return _bad_request(errors[0])

    with metadata_db_write_lock():
        renames, merges = _resolve_merges(col_id, mapping)
        if merges and payload.get("confirm_merges") is not True:
            return _conflict(
                _('This will merge {count} value(s) into existing ones. Continue?')
                .format(count=len(merges)), merges)

        affected = {}
        try:
            for old, new in renames.items():
                row = _value_row(col_id, old)
                if row is None:
                    continue
                for book in row.books:
                    affected[book.id] = book
                    helper.mark_book_modified(book, set_dirty=True)
                row.value = new
            for merge in merges:
                into_row = _value_row(col_id, merge['into'])
                if into_row is None:
                    continue
                for book in _apply_merge(col_id, merge['from'], into_row):
                    affected[book.id] = book
            calibre_db.session.commit()
        except IntegrityError:
            calibre_db.session.rollback()
            return _conflict(_('That value name is already in use'), [])
        except SQLAlchemyError:
            calibre_db.session.rollback()
            log.error("Rename of custom column %s value %r failed", col_id, path)
            return jsonify({"error": {"code": "server_error",
                                      "message": _("The value could not be renamed")}}), 500
        _invalidate_hierarchy_cache()

    _commit_and_log(list(affected.values()))
    return jsonify({
        "renamed": renames, "merged": merges,
        "books": len(affected), "path": path,
    })


# --------------------------------------------------------------------------- #
# Merge (leaves only)
# --------------------------------------------------------------------------- #

@api_v1.route("/columns/<int:col_id>/values/merge", methods=["POST"])
@login_required_if_no_ano
def merge_cc_values(col_id):
    """Fold one or more leaf values into another.

    ``{"paths": ["Arts.Mystery", "Arts.Thriller"], "into": "Arts.Fiction"}``

    Leaves only. A value with children cannot be merged, because the children
    are independent rows and the result would depend on whether they came along
    -- use Move to re-parent instead.

    A SINGLE source is a legitimate request: the editor's per-row "Merge"
    button designates the survivor by clicking its row, so ticking exactly two
    values and pressing Merge on one of them sends one path. That is the same
    operation the multi-select form always expressed, so the source list is
    required to be non-empty rather than to have two or more entries.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    col = _column_or_404(col_id)
    if col is None:
        return _not_found("Column not found")

    payload = request.get_json(silent=True) or {}
    paths = payload.get("paths")
    into = payload.get("into")
    if not isinstance(paths, list) or len(paths) < 1:
        return _bad_request(_('Select at least one value to merge'))
    if not isinstance(into, str) or not into.strip():
        return _bad_request(_('Choose the value to merge into'))
    if any(not isinstance(p, str) or not p.strip() for p in paths):
        return _bad_request(_('Pick valid values to merge'))

    with metadata_db_write_lock():
        tree = calibre_db.get_hierarchical_tree(col_id)
        keep, error = hierarchy.plan_merge(paths, into, *_leaves_of(tree))
        if error is not None:
            return _bad_request(_explain(error))

        into_row = _value_row(col_id, into)
        if into_row is None:
            into_row = db.cc_classes[col_id](value=into)
            calibre_db.session.add(into_row)
            calibre_db.session.flush()

        affected = {}
        try:
            for path in keep:
                for book in _apply_merge(col_id, path, into_row):
                    affected[book.id] = book
            calibre_db.session.commit()
        except SQLAlchemyError:
            calibre_db.session.rollback()
            log.error("Merge in custom column %s failed", col_id)
            return jsonify({"error": {"code": "server_error",
                                      "message": _("The values could not be merged")}}), 500

    _commit_and_log(list(affected.values()))
    return jsonify({"into": into, "merged": keep, "books": len(affected)})


# --------------------------------------------------------------------------- #
# Delete (leaves only)
# --------------------------------------------------------------------------- #

@api_v1.route("/columns/<int:col_id>/values/delete", methods=["POST"])
@login_required_if_no_ano
def delete_cc_values(col_id):
    """Remove leaf values. The books are kept; they simply lose the value.

    ``{"paths": ["Arts.Mystery"]}``

    Leaves only -- a value with children is refused, because deleting it would
    silently change what its children mean. Delete the leaves, or rename/move
    the parent.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    col = _column_or_404(col_id)
    if col is None:
        return _not_found("Column not found")

    payload = request.get_json(silent=True) or {}
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        return _bad_request(_('Select at least one value to delete'))
    if any(not isinstance(p, str) or not p.strip() for p in paths):
        return _bad_request(_('Pick valid values to delete'))

    with metadata_db_write_lock():
        tree = calibre_db.get_hierarchical_tree(col_id)
        keep, error = hierarchy.plan_delete(paths, *_leaves_of(tree))
        if error is not None:
            return _bad_request(_explain(error, 'delete'))

        affected = {}
        removed = []
        try:
            for path in keep:
                row = _value_row(col_id, path)
                if row is None:
                    continue
                for book in list(row.books):
                    getattr(book, _relation(col_id).key).remove(row)
                    affected[book.id] = book
                    helper.mark_book_modified(book, set_dirty=True)
                calibre_db.session.delete(row)
                removed.append(path)
            calibre_db.session.commit()
        except SQLAlchemyError:
            calibre_db.session.rollback()
            log.error("Delete in custom column %s failed", col_id)
            return jsonify({"error": {"code": "server_error",
                                      "message": _("The values could not be deleted")}}), 500
        _invalidate_hierarchy_cache()

    _commit_and_log(list(affected.values()))
    return jsonify({"deleted": removed, "books": len(affected)})


# --------------------------------------------------------------------------- #
# Move
# --------------------------------------------------------------------------- #

@api_v1.route("/columns/<int:col_id>/values/move", methods=["POST"])
@login_required_if_no_ano
def move_cc_value(col_id):
    """Re-parent one node (and its descendants).

    ``{"path": "Arts.Fiction", "new_parent": "History"}``

    There is no separate storage operation for this: a move is a prefix
    rewrite, which is why it is cheap once rename works. Hidden on a flat
    column, which has no hierarchy to move within.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    col = _column_or_404(col_id)
    if col is None:
        return _not_found("Column not found")
    if calibre_db.is_flat_cc_column(col_id):
        return _bad_request(_('This column is a flat list; values cannot be moved.'),
                            code='not_applicable')

    payload = request.get_json(silent=True) or {}
    path = payload.get("path")
    new_parent = payload.get("new_parent", "")
    if not isinstance(path, str) or not path.strip():
        return _bad_request(_('Pick a value to move'))
    if not isinstance(new_parent, str):
        return _bad_request(_('Choose where to move it'))

    path = hierarchy.join_path([path])
    parent = hierarchy.join_path([new_parent]) if new_parent.strip() else ''
    leaf = hierarchy.split_path(path)[-1]
    new_path = hierarchy.join_path([parent, leaf] if parent else [leaf])
    if new_path == path:
        return _bad_request(_('That value is already there'))

    with metadata_db_write_lock():
        node = hierarchy.get_node_by_path(calibre_db.get_hierarchical_tree(col_id), path)
        if node is None:
            return _not_found("Value not found")

        mapping, errors, new_path = _plan_move(col_id, path, parent)
        if errors:
            return _bad_request(errors[0])
        if not mapping:
            # The node resolved above but owns no stored row: nothing to move.
            return _not_found("Value not found")

        renames, merges = _resolve_merges(col_id, mapping)
        if merges:
            # The destination already holds a value this move would land on.
            # Same opt-in rule as rename: report, do not fold.
            return _conflict(
                _('Moving there would merge {count} value(s). Move them one at a time.')
                .format(count=len(merges)), merges)

        affected = {}
        try:
            for old, new in renames.items():
                row = _value_row(col_id, old)
                if row is None:
                    continue
                for book in row.books:
                    affected[book.id] = book
                    helper.mark_book_modified(book, set_dirty=True)
                row.value = new
            calibre_db.session.commit()
        except IntegrityError:
            calibre_db.session.rollback()
            return _conflict(_('A value with that name is already there'), [])
        except SQLAlchemyError:
            calibre_db.session.rollback()
            log.error("Move of custom column %s value %r failed", col_id, path)
            return jsonify({"error": {"code": "server_error",
                                      "message": _("The value could not be moved")}}), 500
        _invalidate_hierarchy_cache()

    _commit_and_log(list(affected.values()))
    return jsonify({"moved": renames, "books": len(affected), "path": new_path})


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #

@api_v1.route("/columns/<int:col_id>/values/create", methods=["POST"])
@login_required_if_no_ano
def create_cc_value(col_id):
    """Create a value, optionally under a parent node.

    ``{"path": "Arts.Mystery"}``

    The new value carries no books until someone assigns one. It is listed
    immediately with a count of 0 because the browse readers join from the
    value table outwards -- see ``CalibreDB.get_hierarchical_tree``.
    """
    guard = _require_metadata_editor()
    if guard is not None:
        return guard
    col = _column_or_404(col_id)
    if col is None:
        return _not_found("Column not found")

    payload = request.get_json(silent=True) or {}
    path = payload.get("path")
    if not isinstance(path, str) or not path.strip():
        return _bad_request(_('Name the value to create'))
    if "," in path:
        return _bad_request(_('A node name cannot contain a comma'))

    hierarchical = not calibre_db.is_flat_cc_column(col_id)
    path = hierarchy.join_path([path]) if hierarchical else path.strip()
    if not path:
        return _bad_request(_('Name cannot be empty'))

    with metadata_db_write_lock():
        if _value_row(col_id, path) is not None:
            return _conflict(_('That value already exists'), [])
        row = db.cc_classes[col_id](value=path)
        calibre_db.session.add(row)
        try:
            calibre_db.session.commit()
        except IntegrityError:
            calibre_db.session.rollback()
            return _conflict(_('That value already exists'), [])
        except SQLAlchemyError:
            calibre_db.session.rollback()
            log.error("Create in custom column %s failed", col_id)
            return jsonify({"error": {"code": "server_error",
                                      "message": _("The value could not be created")}}), 500
        _invalidate_hierarchy_cache()

    return jsonify({"created": path, "id": row.id, "books": 0})