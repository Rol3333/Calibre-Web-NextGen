# SPDX-License-Identifier: GPL-3.0-or-later
"""Classic-side narrowing controls, and their parity with the new interface.

The two implementations of these rules are deliberately duplicated -- they are
view filters with no consequence, and the alternative was dragging React into
the classic page. That duplication is only safe because both sides are pinned:
``frontend/unit/ccEditorNarrowing.test.ts`` holds the TypeScript, and this file
holds the classic script. A change to one without the other fails one of the two
suites, which is the whole point of putting them next to each other.

These are source assertions on the classic side, for the same reason the
existing icon and wiring tests are: a headless browser is not available here and
a fake jQuery would prove nothing about a jQuery implementation.
"""
import os
import re

import pytest

pytestmark = pytest.mark.unit


def _read(*parts):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, os.pardir, os.pardir, *parts),
              encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def script():
    return _read("cps", "static", "js", "cc_value_editor.js")


@pytest.fixture(scope="module")
def partial():
    return _read("cps", "templates", "cc_value_editor.html")


@pytest.fixture(scope="module")
def ts_module():
    return _read("frontend", "src", "lib", "ccNarrow.ts")


# ---------------------------------------------------------------------------
# The rules must agree with the TypeScript
# ---------------------------------------------------------------------------

def test_both_sides_measure_the_focus_the_same_way(script, ts_module):
    """A path-SEGMENT prefix on both sides. `Historyics` merely starts with
    `History` and is a sibling -- a bare string prefix would pull it in."""
    assert "rowPath.indexOf(focusPath + '.') === 0" in script
    assert "path.startsWith(`${focus}.`)" in ts_module


def test_both_sides_include_the_focused_node_itself(script, ts_module):
    assert "rowPath === focusPath" in script
    assert "path === focus" in ts_module


def test_both_sides_measure_depth_from_the_visible_root(script, ts_module):
    """The absolute reading is useless once focused: focus two levels down,
    ask for two levels, and it would show one row."""
    assert "var relative = depth - rootDepth;" in script
    assert "const relative = row.depth - root;" in ts_module


def test_both_sides_clamp_a_negative_relative_depth(script, ts_module):
    assert "relative < 0 ? 0 : relative" in script
    assert "(relative < 0 ? 0 : relative)" in ts_module


def test_both_sides_treat_zero_as_every_level(script, ts_module):
    assert "limit === 0 || depthOf($row) < limit" in script
    assert "if (depthLimit === 0) return true;" in ts_module


def test_a_stale_focus_is_ignored_rather_than_obeyed_by_both(script, ts_module):
    """The bug the tests caught while building it: obeying a focus that matches
    nothing filters every row out and leaves an empty panel."""
    assert "if (!$focused.length)" in script, \
        "the classic boot must drop a focus whose row has gone"
    assert "focusDepth === undefined" in ts_module, \
        "narrowRows must treat an unmatched focus as no focus"


# ---------------------------------------------------------------------------
# Persistence -- a write reloads the page, so state must survive it
# ---------------------------------------------------------------------------

def test_the_narrowing_survives_the_reload_a_write_causes(script):
    """Every write does window.location.reload(), so without sessionStorage the
    filter would throw the user back to the top of a 3,000-value column after
    each rename -- useless for the case it exists for."""
    assert "var focusKey = 'cw-cc-values-focus-' + colId;" in script
    assert "var depthKey = 'cw-cc-values-depth-' + colId;" in script
    assert "sessionStorage.setItem" in script
    # Wrapped: private mode throws on sessionStorage access.
    assert "catch (e) { /* private mode etc. */ }" in script


def test_the_state_is_restored_on_boot_in_the_right_order(script):
    """Focus first, then depth -- and focusing resets the depth, so a stored
    depth is only honoured when there is no stored focus."""
    assert "focusPath = recall(focusKey);" in script
    assert "buildDepthOptions();" in script
    assert "$('#ccValuesDepth').val(focusPath ? '0' : (recall(depthKey) || '0'));" in script


# ---------------------------------------------------------------------------
# Hidden rows must not keep live controls
# ---------------------------------------------------------------------------

def test_row_actions_are_rebuilt_for_the_visible_rows_only(script):
    """A hidden row keeping live buttons means its Cut names a value the user
    cannot see. The per-row builder was split out of render() for exactly this."""
    assert "function renderRowActions($row)" in script
    assert "$rows.filter(':visible').each(function () {" in script
    assert "$editor.find('.cc-values-actions').empty();" in script


def test_changing_a_filter_clears_the_ticks_but_keeps_the_cut_buffer(script):
    """Ticks on rows the user cannot see are invisible state, and merging
    sources you cannot see is the 'panel looks broken' failure this feature has
    had. The cut buffer is NOT cleared: it is a deliberate holding action, the
    banner names it, and its possible destinations are marked among whatever is
    visible."""
    assert "function clearTicks()" in script
    assert re.search(r"function setFocus\([^)]*\)\s*\{[^}]*clearTicks\(\)", script), \
        "focusing must clear the ticks"
    assert re.search(r"function setDepth\([^)]*\)\s*\{[^}]*clearTicks\(\)", script), \
        "changing depth must clear the ticks"
    for setter in ("function setFocus", "function setDepth", "function clearFocus"):
        body = script[script.index(setter):]
        body = body[:body.index("\n        }")]
        assert "cutPath = null" not in body, \
            "%s must not silently discard the cut buffer" % setter


# ---------------------------------------------------------------------------
# Wiring and markup
# ---------------------------------------------------------------------------

def test_the_filter_button_is_delegated_not_bound_per_row(script, partial):
    """These rows are server-rendered and never re-rendered, so a direct
    binding would work until the first narrowing."""
    assert "$rows.on('click', '.cc-values-focus-here'" in script
    assert "cc-values-focus-here" in partial


def test_the_filter_button_is_only_offered_where_it_would_narrow(partial):
    """A leaf is the only row that cannot be narrowed, so the control is not
    rendered there -- a button that does nothing visible is worse than none."""
    assert "{% if row.has_children %}" in partial, \
        "the filter button must be behind has_children"


def test_the_depth_options_are_built_from_the_rows_present(script):
    """A column with nothing below its top level gets 'All levels' only, and the
    select is never empty."""
    assert "function buildDepthOptions()" in script
    assert "if (depth > maxDepth)" in script
    assert "val('0').text(t('allLevels'))" in script
    assert "for (var level = 1; level <= maxDepth + 1; level += 1)" in script


def test_the_hidden_count_is_reported(script):
    """Silently hiding 2,000 rows leaves the user thinking the column is
    smaller than it is, or that the click did nothing."""
    assert "t('hiddenByNarrowing'" in script
    assert "totalRows" in script


def test_focusing_resets_the_depth(script):
    """The stored limit was measured from a different root. Keeping it is how a
    user ends up looking at one row and concluding the filter is broken."""
    assert re.search(
        r"function setFocus\([^)]*\)\s*\{[^}]*\$\('#ccValuesDepth'\)\.val\('0'\)", script)


def test_the_icon_exists_in_the_shipped_bootstrap(partial):
    """`glyphicon-filter` is checked against the stylesheet that ships, for the
    same reason `clipboard` and `compress` were not: an invented glyphicon name
    renders a BLANK button."""
    css = _read("cps", "static", "css", "libs", "bootstrap.min.css")
    assert "glyphicon-filter" in partial
    assert '.glyphicon-filter:before' in css


def test_every_new_string_is_served_by_the_partial(partial, script):
    """A t() key with no entry in data-strings renders as the raw key, so a
    tooltip would say 'allLevels'."""
    keys = set(re.findall(r"t\('([a-zA-Z]+)'", script))
    narrowing = {"allLevels", "oneLevel", "manyLevels", "hiddenByNarrowing"}
    assert narrowing <= keys, "the script must ask for these keys"
    for key in narrowing:
        assert '"%s"' % key in partial, "%s has no entry in data-strings" % key


def test_no_plural_kwarg_is_passed_to_a_t_without_plural_support(partial):
    """The classic t() is a plain {name} substitution. flask-babel's count=
    would pick a plural form and then leave {count} unsubstituted, so the
    narrowing strings use two keys instead of one pluralised one."""
    for match in re.finditer(r'_\("([^"]*)"[^)]*count=', partial):
        assert "{count}" in match.group(1) or "{" not in match.group(1), (
            "count= is passed but this blob cannot substitute it: %r" % match.group(1))
    assert '"oneLevel"' in partial and '"manyLevels"' in partial, \
        "two strings is honest English instead of '1 level(s)'"


def test_no_jinja_comment_lives_inside_the_strings_blob(partial):
    """A {# ... #} inside the data-strings attribute breaks the JSON, and one
    containing a double quote breaks the attribute too. Found the hard way."""
    start = partial.index("data-strings=")
    end = partial.index("|tojson }}", start)
    blob = partial[start:end]
    assert "{#" not in blob and "#}" not in blob, (
        "a Jinja comment inside the strings blob will not parse")
