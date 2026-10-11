# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 5 -- move up/down in both clients.

The server half already worked: ``can_move_up``/``can_move_down`` are published
on every node and ``POST .../values/reorder`` exists. What is missing is the
part a person can actually touch.

Two things could have gone wrong and both are silent:

* A client that **derives** the position instead of reading the published flag.
  Reordering is exactly the rule the single-home refactor exists to stop being
  duplicated, so a TypeScript copy of it would be a regression of the whole
  point -- and unlike the cycle rule it is not even necessary, because the
  answer turns only on sibling position, which the server knows.
* A client that forgets the **tick gate**, offering a live button that the
  endpoint refuses, which is the "working-looking panel whose every action is
  refused" failure this feature already had once.
"""
import os

import pytest

pytestmark = pytest.mark.unit


def _read(*parts):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, os.pardir, os.pardir, *parts),
              encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def editor_js():
    return _read("cps", "static", "js", "cc_value_editor.js")


@pytest.fixture(scope="module")
def editor_html():
    return _read("cps", "templates", "cc_value_editor.html")


@pytest.fixture(scope="module")
def editor_tsx():
    return _read("frontend", "src", "pages", "CcValueEditor.tsx")


@pytest.fixture(scope="module")
def api_types():
    return _read("frontend", "src", "lib", "api.ts")


# ---------------------------------------------------------------------------
# The icons have to exist, or the buttons render blank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("icon,why", [
    ("arrow-up", "Move up"),
    ("arrow-down", "Move down"),
])
def test_every_row_icon_exists_in_the_shipped_bootstrap(editor_js, icon, why):
    """A glyphicon this Bootstrap does not define renders as a BLANK button.

    Not hypothetical: `clipboard` and `compress` were invented names, so Paste
    and Merge had no picture while looking like ordinary disabled buttons.
    Checked against the stylesheet actually shipped, not the version assumed.
    """
    css = _read("cps", "static", "css", "libs", "bootstrap.min.css")

    assert '.glyphicon-%s:before' % icon in css, (
        'glyphicon-%s (%s) does not exist in this Bootstrap build -- the button '
        'would render blank' % (icon, why))
    assert "action('%s'" % icon in editor_js, (
        '%s must actually be the icon wired for %s' % (icon, why))


# ---------------------------------------------------------------------------
# Classic: the row carries the flag, the button reads it
# ---------------------------------------------------------------------------

def test_the_row_renders_both_move_flags(editor_html):
    """Server-rendered, like every other flag on this row: the rows and their
    availability arrive as markup, not as a fetch."""
    for flag in ("data-can-move-up", "data-can-move-down"):
        assert flag in editor_html, (
            "%s must be on the row -- the alternative is deriving it in the "
            "browser, which is what the single-home refactor removed" % flag)
        assert "row.can_move_up" in editor_html
        assert "row.can_move_down" in editor_html


def test_the_classic_buttons_are_gated_on_the_tick(editor_js):
    """Both halves of the gate, not one. A button that is enabled before the
    row is ticked looks like a broken control."""
    assert editor_js.count("canMoveUp && isChecked(path)") == 1, \
        "Move up must require BOTH the published flag and a tick"
    assert editor_js.count("canMoveDown && isChecked(path)") == 1, \
        "Move down must require BOTH the published flag and a tick"


def test_the_classic_buttons_are_hidden_on_a_flat_column(editor_js):
    """A flat column is one level, so reordering is coherent -- but the
    decision taken was to follow the Paste precedent and keep it hierarchical
    only for the first cut."""
    for icon in ("arrow-up", "arrow-down"):
        at = editor_js.index("action('%s'" % icon)
        window = editor_js[max(0, at - 400):at]
        assert "hierarchical ?" in window, (
            "%s must be behind the same `hierarchical` guard as Paste" % icon)


def test_the_classic_client_calls_the_reorder_endpoint_with_the_direction(editor_js):
    assert "write('reorder'" in editor_js
    assert "direction: direction" in editor_js
    # And the direction is the one the button pressed, not a derived guess.
    assert "runReorder(path, 'up')" in editor_js
    assert "runReorder(path, 'down')" in editor_js


def test_the_classic_client_does_not_derive_sibling_position(editor_js):
    """The whole point. `runReorder` must forward the path and the direction and
    nothing else.

    Scoped to that function on purpose: the file legitimately contains a client
    copy of the *cycle* rule (`indexOf(cut + '.')` in `canPasteInto`), which is
    a different question -- whether a row may RECEIVE a value -- and cannot be
    published because it depends on what the user is holding. A blanket ban on
    those characters would have flagged correct code.
    """
    at = editor_js.index("function runReorder")
    body = editor_js[at:at + 320]

    assert "write('reorder'" in body
    for banned in ("children", "indexOf", "prev", "next", "sibling", "length"):
        assert banned not in body, (
            "runReorder must not work out a position (%r): the server published "
            "it" % banned)


def test_every_move_string_is_served_by_the_partial(editor_html, editor_js):
    """A `t()` key with no entry in data-strings renders as the raw key, which
    is how a tooltip ends up saying 'tickToMove'."""
    import re

    keys = set(re.findall(r"t\('([a-zA-Z]+)'", editor_js))
    # Keys the script defines locally rather than in the blob.
    local = {"cut", "paste", "mergeInto", "rename", "delete"}
    missing = {k for k in keys if k not in local and '"%s"' % k not in editor_html}

    assert not missing, "t() keys with no entry in data-strings: %s" % sorted(missing)


def test_reorder_asks_for_no_confirmation(editor_js):
    """It touches no book and rewrites no value, so a confirm dialog would be
    noise -- and merge and delete both confirm, so its absence is meaningful.

    Scoped to the function body only: `runMove` follows it and does confirm.
    """
    at = editor_js.index("function runReorder")
    body = editor_js[at:editor_js.index("function runMove", at)]

    assert "window.confirm" not in body, "reordering needs no confirmation"
    # ...and the operations that DO destroy something still do confirm.
    for destructive in ("runMerge", "runDelete"):
        start = editor_js.index("function %s" % destructive)
        tail = editor_js[start:start + 400]
        assert "window.confirm" in tail, "%s must keep its confirmation" % destructive


# ---------------------------------------------------------------------------
# SPA: same endpoint, same gate, still no derivation
# ---------------------------------------------------------------------------

def test_the_spa_wire_type_carries_the_move_flags(api_types):
    for field in ("can_move_up", "move_up_reason", "can_move_down",
                  "move_down_reason"):
        assert field in api_types, "%s must be on CcNodeAvailability" % field


def test_the_spa_renders_both_buttons_behind_the_hierarchical_guard(editor_tsx):
    for icon in ("ChevronUp", "ChevronDown"):
        assert icon in editor_tsx, "%s must be imported" % icon
    # Each button's own opening fragment must carry the guard, so hiding one
    # cannot leave the other live on a flat column.
    for icon in ("ChevronUp", "ChevronDown"):
        at = editor_tsx.index("<%s " % icon)
        opening = editor_tsx[max(0, at - 600):at]
        assert "hierarchical && (" in opening, (
            "%s must sit behind the same `hierarchical &&` guard as Paste" % icon)


def test_the_spa_buttons_read_the_published_flag_and_gate_on_the_tick(editor_tsx):
    assert "can_move_up === true" in editor_tsx, \
        "the position verdict must be READ, not derived"
    assert "can_move_down === true" in editor_tsx
    # The tick gate, on both buttons.
    assert editor_tsx.count("disabled={busy || !isTicked || !moveState(node,") == 2


def test_the_spa_calls_the_reorder_endpoint(editor_tsx):
    assert "useReorderCcValue" in editor_tsx
    assert "runReorder" in editor_tsx
    assert "runReorder(node.path, 'up')" in editor_tsx
    assert "runReorder(node.path, 'down')" in editor_tsx


def test_the_spa_does_not_derive_sibling_position(editor_tsx):
    for banned in ("children.length === 0 &&", "indexOf(cutPath", "siblingIndex"):
        assert banned not in editor_tsx, (
            "%s looks like a client-side derivation of sibling position" % banned)


def test_the_spa_keeps_the_optional_flag_optional(editor_tsx):
    """A server older than the flags publishes neither, and the buttons must
    stay hidden rather than appear enabled and be refused."""
    assert "node.available?.can_move_up === true" in editor_tsx, (
        "the flags are optional in the type; reading them must be optional too, "
        "so a mixed-version rollout shows no button rather than a dead one")


def test_both_clients_reach_the_same_endpoint(editor_js, editor_tsx):
    """Two interfaces, one implementation of the operation -- the same property
    the whole single-home refactor was built for."""
    assert "'/api/v1/columns/' + colId + '/values/' + action" in editor_js
    assert '/api/v1/columns/${colId}/values/reorder' in _read(
        "frontend", "src", "lib", "queries.ts")


def test_a_409_is_explained_rather_than_shown_as_a_failure(editor_tsx):
    """A stale tree is the one legitimate way the published flag and the server
    can disagree. Saying so beats doing nothing quietly."""
    assert "already at the top" in editor_tsx or "Already first at this level" in editor_tsx
    assert "status === 409" in editor_tsx
