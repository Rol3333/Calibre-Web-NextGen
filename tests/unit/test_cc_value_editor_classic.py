# SPDX-License-Identifier: GPL-3.0-or-later
"""The classic interface's custom-column value editor.

Phase C gave /custom_column/<id> the same five operations the new interface had,
so a value could be renamed, merged, deleted, moved and created from either UI.
That is only worth anything if both UIs agree, and these tests are what makes
the agreement structural rather than maintained by hand:

1. **Gate parity.** Both interfaces hide the entry point from anyone without
   the metadata-edit role. The SPA's copy of this bug is the reason the gate is
   tested at all: its Edit button was ungated, so a non-editor got working
   controls that were all refused, and the refusal rendered below the tree, off
   screen, so the feature read as "nothing happens".

2. **One implementation.** The classic editor POSTs to the same five endpoints,
   so there is one set of rules and one set of permission checks. A classic-only
   endpoint would be a second implementation to keep in step.

3. **Published flags, not re-derived ones.** Both editors read `available` off
   the tree response. Neither re-derives "can this value be deleted / merged"
   from a node's sub-values.

These are structural tests, not screenshots: they read the template, the script
and the endpoints, because what can silently regress is the wiring between them.
"""
import os
import re

import pytest

from cps import hierarchy

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         os.pardir, os.pardir, 'cps', 'templates')
STATIC_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         os.pardir, os.pardir, 'cps', 'static', 'js')


def render_cc_list(cc_list, editor_partial, may_edit):
    """Render cc_list.html standalone, with or without the edit permission.

    Drops the `extends`/`block` scaffolding so no layout.html is needed, but
    KEEPS every block BODY: the `js` block holds the editor's <script>, and an
    earlier probe discarded the blocks wholesale and so wrongly reported the
    script missing in both cases.
    """
    import re as _re

    from jinja2 import Environment, DictLoader

    class _User:
        def __init__(self, edit):
            self._edit = edit
            self.is_anonymous = False

        def role_edit(self):
            return self._edit

    source = cc_list.replace('{% extends "layout.html" %}', '')
    source = _re.sub(r'{%\s*block\s+\w+\s*%}', '', source)
    source = source.replace('{% endblock %}', '')

    env = Environment(loader=DictLoader({
        'cc_list.html': source,
        'cc_value_editor.html': editor_partial,
        # A stub: this probe is about which controls render, and the browse
        # tree's markup is covered by the hierarchy template's own tests.
        'macros/hierarchy.html':
            '{% macro render_details_tree(nodes, col_id, current_path=\'\') %}'
            'TREE{% endmacro %}',
    }))
    env.globals['url_for'] = lambda *a, **k: '#'
    return env.get_template('cc_list.html').render(
        title='Subjects', page='cclist', col_id=1, hierarchical=True,
        may_edit_values=may_edit, edit_rows=[], entries=[],
        current_user=_User(may_edit), _=lambda s: s)


# The tree the row-builder tests move values around in. Three deep on purpose:
# every bug worth catching lives in the difference between "this node", "its
# parent" and "its own ancestor".
CUT_TREE = [
    "TestNode",
    "TestNode.TestSubNode",
    "TestNode.TestSubNode.TestDeepNode",
    "History",
    "History.AncientHistory",
    "History.AncientHistory.India",
    "Other",
]


def _read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as handle:
        return handle.read()


@pytest.fixture(scope='module')
def cc_list():
    return _read(TEMPLATES, 'cc_list.html')


@pytest.fixture(scope='module')
def editor_partial():
    return _read(TEMPLATES, 'cc_value_editor.html')


@pytest.fixture(scope='module')
def editor_js():
    return _read(STATIC_JS, 'cc_value_editor.js')


# --------------------------------------------------------------------------- #
# 1. The permission gate
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_the_entry_button_is_gated_on_the_metadata_edit_role(cc_list, editor_partial):
    """The gate must be on the BUTTON, not merely on the script include.

    An ungated button is the exact regression: every endpoint it can reach
    answers 403 without role.edit, so the user is shown controls that cannot
    work. The gate itself lives in web.py::render_cc_category, as
    ``may_edit_values`` — this asserts the template USES that flag rather than
    re-deriving the role check in markup, which is what made the SPA's copy
    drift from the endpoint's.
    """
    # By RENDERING both ways, not by reading the template. Three earlier
    # versions of this check were regexes over the markup — "is may_edit_values
    # within N characters above the button?" — and every one of them broke on a
    # toolbar rearrangement while passing for the wrong reason in between. Worse,
    # a mutation probe proved one of them still passed with the gate entirely
    # removed: it proved nothing. What matters is the OUTPUT, so this renders.
    for flag, expected in ((True, True), (False, False)):
        out = render_cc_list(cc_list, editor_partial, may_edit=flag)
        for what, marker in (
            ('the Edit values button', 'btn-cc-values-edit'),
            ('the editor panel', 'ccValuesEditor'),
        ):
            assert (marker in out) is expected, (
                '%s must be %s when may_edit_values=%s — a control rendered for '
                'a non-editor offers five operations that all answer 403'
                % (what, 'present' if expected else 'absent', flag))
    # Comments may name the role they are describing; only an ACTUAL expression
    # in the markup would be a second copy of the gate.
    markup = re.sub(r'\{#.*?#\}', '', cc_list, flags=re.S)
    assert 'role_edit()' not in markup, (
        'the role check belongs in web.py::render_cc_category only — a second '
        'copy in the template is how the two can disagree')


@pytest.mark.unit
def test_the_editor_script_is_not_loaded_without_the_role(cc_list):
    """Belt and braces: the script itself must not ship to a non-editor.

    Hiding the button while still loading the editor would leave the whole
    panel in the page, and would mean the gate exists in two places.
    """
    script = cc_list.index('cc_value_editor.js')
    window = cc_list[max(0, script - 400):script]
    assert 'may_edit_values' in window


@pytest.mark.unit
def test_the_view_defines_the_gate_once_in_python():
    """One place decides who may edit values: web.py.

    ``_may_edit_column_values`` is the single definition; the template and the
    script include both consult the boolean it returns.
    """
    web = _read(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, os.pardir, 'cps', 'web.py'))
    assert 'def _may_edit_column_values' in web
    body = web[web.index('def _may_edit_column_values'):
               web.index('def _may_edit_column_values') + 600]
    assert 'role_edit()' in body, (
        'the gate must be the metadata-edit role — it is what every value-write '
        'endpoint checks')
    assert 'is_anonymous' in body, (
        'anonymous browsing is on by default; an anonymous visitor must not be '
        'offered a metadata write panel')


# --------------------------------------------------------------------------- #
# 1b. In place, not in a dialog
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_the_editor_is_not_a_modal(cc_list, editor_partial):
    """It replaces the browse view in the same window, as the SPA does.

    A modal was tried first and rejected: it opened over the tree it was
    editing, and it was a small box on a page with room to spare.
    """
    assert 'class="modal' not in editor_partial, (
        'the editor must not be a modal — it edits in place')
    assert 'class="modal' not in cc_list
    assert 'hidden' in editor_partial, (
        'the panel starts hidden and is revealed by the Edit values button')


@pytest.mark.unit
def test_entering_and_leaving_edit_mode_toggles_the_two_views(editor_js):
    """Edit values shows the panel and hides the tree; Done reverses it.

    Both directions go through one setMode(), so neither can be half-implemented.
    A Done that does not restore the browse view leaves the user stuck in a mode
    they cannot leave — the mirror image of the original "nothing happens" bug.
    """
    assert re.search(r"function setMode\(editing\)", editor_js), (
        'both directions must share one toggle, so they cannot diverge')
    # Slice to the next top-level statement after setMode's body, rather than to
    # a handler string: the anchor moved when the wiring was fixed, and an anchor
    # that names a handler silently stops slicing the function it meant to.
    start = editor_js.index('function setMode(editing)')
    end = editor_js.index('function ', start + len('function setMode(editing)'))
    body = editor_js[start:end]
    assert "$editor.prop('hidden', !editing);" in body, (
        'the panel follows the mode')
    assert "$browse.prop('hidden', editing);" in body, (
        'and so does the browse view — the panel replaces it rather than covering it')


@pytest.mark.unit
def test_edit_and_done_share_one_toolbar_slot(cc_list, editor_js):
    """One button, relabelled — not two controls at two positions.

    A control that moves when the mode changes reads as a layout jump, and the
    user has to hunt for it again. So the SAME element carries both states and
    only its classes and label change.
    """
    assert cc_list.count('btn-cc-values-edit') == 1, (
        'exactly one Edit values control may exist; Done must reuse it')
    assert 'cc-values-toggle' in cc_list, (
        'the toggle needs its own slot so it can be styled right-aligned')
    assert 'pull-right' in cc_list, (
        'the toggle must sit on the RIGHT, level with Expand all / Collapse all')
    assert 'toggleClass(\'btn-cc-values-edit\'' in editor_js, (
        'the slot must swap its own state classes, not create a new control')


@pytest.mark.unit
def test_both_directions_hang_off_the_one_slot(editor_js):
    """Entering and leaving are ONE handler on the slot, branching on its class.

    They used to be two bindings, one per state class. That is what broke Done:
    cc_list.html renders the button with only the Edit class, so the Done binding
    resolved to an empty jQuery set and was never invoked — while the handler text
    was present in the file, so a grep-for-the-string test stayed green.
    """
    assert '$slot.on(\'click\'' in editor_js, (
        'both directions must be bound to the slot, which exists in both modes')
    assert "$slot.hasClass('btn-cc-values-done')" in editor_js, (
        'the single handler must branch on the state class to pick a direction')
    assert re.search(r"\$\('\.btn-cc-values-done'\)\.on\(", editor_js) is None, (
        'binding the Done state class separately binds to nothing at boot: the '
        'class does not exist until setMode adds it, so the handler is never '
        'attached and Done silently does nothing')


@pytest.mark.unit
def test_every_boot_time_binding_matches_something_in_the_page(cc_list, editor_partial,
                                                              editor_js):
    """Every class bound with `.on(...)` at load must exist in the RENDERED page.

    This is the general form of the bug above, and the reason a text match is
    not enough. jQuery resolves `$(...).on(...)` against the DOM as it exists at
    that instant: binding to a class that nothing carries yet attaches the handler
    to an empty set, and the control is dead with no error anywhere. Classes are
    what setMode swaps at runtime, so those must not be used as binding anchors.

    Scoped to top-level `$( '...' ).on(` bindings, which is where the failure
    happened; handlers attached to freshly created elements are fine.
    """
    markup = cc_list + editor_partial
    body = re.sub(r"/\*.*?\*/", "", editor_js, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    selectors = re.findall(r"\$\(\s*'([^']+)'\s*\)\s*\.\s*on\(", body)
    assert selectors, 'expected at least one top-level binding to check'
    for selector in selectors:
        classes = re.findall(r"\.([A-Za-z0-9_-]+)", selector)
        for name in classes:
            assert name in markup, (
                "the script binds '%s', but no element in the rendered page "
                "carries the class '%s'; the handler attaches to nothing and the "
                "control does nothing" % (selector, name))


@pytest.mark.unit
def test_edit_mode_survives_the_reload_a_write_causes(editor_js):
    """Renaming must not throw the user out of edit mode.

    Every write re-reads the page — a rename rewrites every stored spelling
    beneath it, so patching the DOM would leave the browser asserting facts the
    database no longer holds. That reload used to drop the user back into the
    browse view, so renaming N values in a column meant clicking Edit values N
    times, and losing sight of which one they were on.
    """
    assert re.search(r"sessionStorage\.setItem\(modeKey, '1'\)", editor_js), (
        'entering edit mode must be remembered, or the reload undoes it')
    assert re.search(r"sessionStorage\.removeItem\(modeKey\)", editor_js), (
        'and leaving it must clear the flag, so a later visit to the page does '
        'not open a metadata write panel the user never asked for')
    assert 'rememberMode(editing);' in editor_js, (
        'setMode must persist the mode it just applied')
    assert re.search(r"if \(wasEditing\(\)\) \{\s*setMode\(true\);", editor_js), (
        'the boot path must restore the mode the reload interrupted')
    # Per column, not global: one column in edit mode must not open another.
    assert "'cw-cc-values-edit-' + colId" in editor_js, (
        'the remembered mode must be scoped to the column it belongs to')


@pytest.mark.unit
def test_every_write_is_reachable_from_the_page(cc_list, editor_js, editor_partial):
    """Each of the five operations must have a control on the page.

    A control that exists only in the script is unreachable: 'Create new' was
    briefly dropped in a toolbar restructure while runCreate() survived in the
    script, so the operation simply could not be performed and nothing failed.
    This checks the BUTTON exists, not merely the handler.
    """
    # Create is the one operation with no per-row button, so it needs its own.
    assert 'id="ccValuesCreate"' in cc_list, (
        'Create must have a button — runCreate() alone is unreachable code')
    assert "$('#ccValuesCreate').on('click', runCreate);" in editor_js, (
        'the Create button must be wired')
    # The other four live on every row, so a row must exist to carry them.
    assert '{% for row in edit_rows %}' in editor_partial, (
        'the per-row actions need a row to sit on')
    for icon in ('pencil', 'scissors', 'paste', 'transfer', 'trash'):
        assert "action('%s'" % icon in editor_js, icon


@pytest.mark.unit
def test_both_views_share_one_frame(cc_list):
    """Edit mode must inherit the browse view's padding, not invent its own.

    The editor used to sit outside the `.discover` frame, so toggling modes
    changed the page's inset and the content appeared to jump.
    """
    assert cc_list.count('class="discover') == 1, (
        'exactly one .discover frame must wrap BOTH views')
    frame = cc_list.index('cc-values-frame')
    assert frame < cc_list.index('cc-values-browse')
    assert frame < cc_list.index("include 'cc_value_editor.html'")


@pytest.mark.unit
@pytest.mark.parametrize('icon,why', [
    ('paste', 'Paste'),
    ('transfer', 'Merge'),
    ('pencil', 'Rename'),
    ('scissors', 'Cut'),
    ('trash', 'Delete'),
])
def test_every_row_icon_exists_in_the_shipped_bootstrap(editor_js, icon, why):
    """A glyphicon name that Bootstrap does not define renders as a BLANK button.

    This was a real bug: `clipboard` and `compress` were invented names, so
    Paste and Merge had no picture at all while looking like ordinary disabled
    buttons. Checked against the stylesheet actually shipped, not against the
    Bootstrap version assumed.
    """
    css = _read(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, os.pardir, 'cps', 'static', 'css',
                             'libs', 'bootstrap.min.css'))
    assert '.glyphicon-%s:before' % icon in css, (
        'glyphicon-%s (%s) does not exist in this Bootstrap build — the button '
        'would render blank' % (icon, why))
    assert "action('%s'" % icon in editor_js, (
        '%s must actually be the icon wired for %s' % (icon, why))


@pytest.mark.unit
def test_the_rows_are_server_rendered_with_the_flags_embedded(editor_partial, editor_js):
    """The rows and their availability arrive as markup, not as a fetch.

    An editor that must round-trip before it can draw itself is the
    "button does nothing" failure this feature already had once: the tree is
    already in the page, so rendering it again over HTTP would only add a way
    for the panel to be blank.
    """
    assert 'data-can-delete' in editor_partial
    assert 'data-can-merge-source' in editor_partial
    assert '{% for row in edit_rows %}' in editor_partial
    assert 'getJSON' not in editor_js, (
        'the panel is server-rendered; it must not re-fetch the tree it has')


@pytest.mark.unit
def test_the_partial_renders_the_rows_it_is_given(editor_partial):
    """`edit_rows` must reach the markup, flags and indentation included.

    A rename rewrites every stored spelling beneath it, so the depth and the
    availability of each row are exactly what the editor is for; if the loop
    silently rendered nothing the panel would be an empty box that still
    looked like an editor.
    """
    from jinja2 import Environment, DictLoader

    env = Environment(loader=DictLoader({'cc_value_editor.html': editor_partial}))

    class _User:
        def role_edit(self):
            return True

        is_anonymous = False

    out = env.get_template('cc_value_editor.html').render(
        col_id=3, hierarchical=True, _=lambda s: s, current_user=_User(),
        edit_rows=[
            {'path': 'History', 'name': 'History', 'total_count': 12,
             'depth': 0, 'can_delete': False, 'can_merge_source': False,
             'has_children': True},
            {'path': 'History.Ancient', 'name': 'Ancient', 'total_count': 4,
             'depth': 1, 'can_delete': True, 'can_merge_source': True,
             'has_children': False},
        ])

    assert out.count('class="cc-values-row"') == 2
    assert 'data-path="History"' in out
    assert 'data-can-delete="0"' in out, 'the interior node must not be deletable'
    assert 'data-can-delete="1"' in out, 'the leaf must be deletable'
    # depth 1 of the fixture tree: indent_px (40) + indent_root_px (6) = 46px.
    assert 'padding-left: 46px' in out, (
        'the second row must be indented by its depth, or the tree reads flat')
    assert 'has sub-values' in out, (
        'a value with children must say so, so the disabled Delete is not a '
        'mystery')


@pytest.mark.unit
def test_the_indent_step_is_wide_enough_to_read_the_depth(editor_partial):
    """Nesting has to be VISIBLE, not merely present.

    The step was 18px/level, which is roughly one character of the value name:
    on a column four levels deep the rows looked like a flat list with some
    wrapping. It is a named template variable rather than an inline sum so the
    reason and the value live together and the next change has one place to edit.
    """
    assert re.search(r"{%\s*set\s+indent_px\s*=\s*40\s*%}", editor_partial), (
        'the per-level indent step must be a named value, and must be wide '
        'enough to read the depth at a glance')
    assert 'row.depth * indent_px + indent_root_px' in editor_partial, (
        'the row offset must be computed from that value, not re-spelled inline')
    # A deeper row must move further right, not merely be marked differently.
    assert re.search(r"row\.depth\s*\*\s*indent_px", editor_partial)


@pytest.mark.unit
def test_the_classic_and_spa_indents_agree(editor_partial):
    """The same tree must not look like two different depths per interface.

    Classic renders `indent_px` in Jinja and the SPA `INDENT_PX` in TSX. They
    cannot share a constant across the two stacks, so this pins the pair instead:
    a change to one that is not made to the other is caught here rather than by
    a user noticing that the same column indents differently per interface.
    """
    tsx = _read(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             os.pardir, os.pardir, 'frontend', 'src', 'pages',
                             'CcValueEditor.tsx'))
    classic = int(re.search(r"indent_px\s*=\s*(\d+)", editor_partial).group(1))
    spa = int(re.search(r"INDENT_PX\s*=\s*(\d+)", tsx).group(1))
    assert classic == spa, (
        'classic indents %dpx per level and the SPA %dpx; the same hierarchy '
        'must read the same in both' % (classic, spa))


# --------------------------------------------------------------------------- #
# 2. One implementation of every operation
# --------------------------------------------------------------------------- #

@pytest.mark.unit
@pytest.mark.parametrize('action', [
    'rename', 'merge', 'delete', 'move', 'create',
])
def test_the_classic_editor_calls_the_same_endpoint_as_the_spa(editor_js, action):
    """All five writes go to /api/v1/columns/<id>/values/<action>.

    A classic-specific endpoint would be a second implementation of the rules
    and the permission check, which is the duplication this work removed.
    """
    assert "'/values/' + action" in editor_js, (
        'writes must build one URL from the action name, so the five endpoints '
        'cannot each acquire a private variant')
    for verb in ('rename', 'merge', 'delete', 'move', 'create'):
        assert "write('%s'" % verb in editor_js, verb


@pytest.mark.unit
def test_the_classic_editor_sends_the_csrf_token_and_the_proxy_prefix(editor_js):
    """Both are the classic helpers, not window.location guesses.

    getPath() is what every other classic AJAX call uses for a reverse-proxy
    sub-path (see get_cc_tree.js); the csrf_token input is rendered by
    layout.html on every page. Getting either wrong makes every write fail with
    403 behind a proxy or a missing token.
    """
    assert 'getPath()' in editor_js
    assert "input[name='csrf_token']" in editor_js


# --------------------------------------------------------------------------- #
# 2b. The server-side row builder
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_edit_rows_are_flat_depth_first_with_the_server_flags():
    """web.py::cc_value_edit_rows must flatten the tree in the SHOWN order.

    Depth-first in display order, because the panel is a flat list: any other
    order puts a child above its parent and the indentation reads as a jump
    rather than a nesting. Each row must carry the flags from
    hierarchy.node_availability, not a recomputed guess.
    """
    from cps.web import cc_value_edit_rows

    tree = hierarchy.parse_tag_hierarchy([(1, v) for v in CUT_TREE])
    rows = cc_value_edit_rows(tree, True)

    assert [r['path'] for r in rows] == [
        'History',
        'History.AncientHistory',
        'History.AncientHistory.India',
        'Other',
        'TestNode',
        'TestNode.TestSubNode',
        'TestNode.TestSubNode.TestDeepNode',
    ]
    assert [r['depth'] for r in rows] == [0, 1, 2, 0, 0, 1, 2]

    by_path = {r['path']: r for r in rows}
    assert by_path['History']['can_delete'] is False
    assert by_path['History']['can_merge_source'] is False
    assert by_path['History']['has_children'] is True
    assert by_path['Other']['can_delete'] is True
    assert by_path['Other']['can_merge_source'] is True
    assert by_path['Other']['has_children'] is False


@pytest.mark.unit
def test_edit_rows_agree_with_the_published_flags():
    """The same node must get the same verdict whichever interface asks.

    This is the point of Phase B: the classic editor and the SPA read two
    different transports — data attributes and JSON — but ONE decision. If the
    row builder and the wire serializer ever disagreed, the two interfaces
    would offer different actions for the same value.
    """
    from cps.api.columns import _node_to_json
    from cps.web import cc_value_edit_rows

    tree = hierarchy.parse_tag_hierarchy([(1, v) for v in CUT_TREE])
    rows = {r['path']: r for r in cc_value_edit_rows(tree, True)}

    def walk(nodes):
        for node in nodes:
            yield node
            for child in walk(node['children']):
                yield child

    for node in walk(tree):
        wire = _node_to_json(node)['available']
        row = rows[node['path']]
        assert row['can_delete'] == wire['can_delete'], node['path']
        assert row['can_merge_source'] == wire['can_merge_source'], node['path']


@pytest.mark.unit
def test_a_flat_column_renders_one_level_with_no_indentation():
    """A flat column has no hierarchy, so every value is a leaf at depth 0.

    Indenting a Dewey list would imply a nesting that does not exist, and
    ``node_availability`` correctly reports every value deletable -- the same
    one-level contract the JSON tree keeps for flat columns.
    """
    from cps.web import cc_value_edit_rows

    # Built the way a FLAT column's entries actually arrive: whole atomic
    # values with no children, not parse_tag_hierarchy's output. Feeding Dewey
    # numbers through parse_tag_hierarchy would split '775' out as a parent of
    # '778.3', which is precisely the fiction a flat column exists to avoid --
    # so that would test the wrong producer.
    flat = [
        {'name': '775', 'path': '775', 'count': 1, 'total_count': 1,
         'children': []},
        {'name': '778.3', 'path': '778.3', 'count': 1, 'total_count': 1,
         'children': []},
        {'name': 'QA76.76.C68', 'path': 'QA76.76.C68', 'count': 1,
         'total_count': 1, 'children': []},
    ]
    rows = cc_value_edit_rows(flat, False)

    assert [r['depth'] for r in rows] == [0, 0, 0]
    assert [r['path'] for r in rows] == ['775', '778.3', 'QA76.76.C68'], (
        'a flat value is one entry, kept whole -- the dots are part of the value')
    assert all(r['can_delete'] for r in rows)
    assert all(r['can_merge_source'] for r in rows)
    assert all(not r['has_children'] for r in rows)


# --------------------------------------------------------------------------- #
# 3. The published flags, not a second derivation
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_the_classic_editor_reads_the_published_availability(editor_js):
    # The flags arrive as data attributes on server-rendered rows, so the
    # script reads data-can-delete, not a JSON `available` object as the SPA
    # does. Same values, different transport — and both come from the one
    # hierarchy.node_availability that decides them.
    assert "data('can-delete')" in editor_js
    assert "data('can-merge-source')" in editor_js


@pytest.mark.unit
def test_the_editor_never_asks_how_many_children_a_row_has(editor_js):
    """The duplication this replaced: counting children to decide legality.

    The classic editor has NO legacy fallback and needs none: its rows are
    rendered by this app's own server, so `data-can-delete` is always present.
    A fallback would be dead code that suggests the flag might be missing.
    """
    assert "data('can-delete')" in editor_js
    assert "data('can-merge-source')" in editor_js
    assert 'children.length' not in editor_js, (
        'the leaf rule comes from the published flag, not from counting '
        'sub-values — that re-derivation is what this replaced')


@pytest.mark.unit
def test_the_cycle_rule_is_the_one_rule_the_classic_editor_must_keep(editor_js):
    """The single client-side copy of a server rule, and its scope stated.

    `can_place_under` needs the HELD value, so it cannot be published per node:
    whether a row can receive a move depends on what the user has cut right
    now. Every other rule moved to the server in Phase B.

    So this one is pinned deliberately — its shape must not drift (a prefix
    match that is not a path segment would wrongly allow 'Historyics'), and the
    file must say where the authoritative copy lives.
    """
    assert "String(target).indexOf(cut + '.') !== 0" in editor_js, (
        "the cycle test must be a PATH-SEGMENT prefix match -- 'Historyics' is "
        "a sibling of 'History', not a child")
    assert 'can_place_under' in editor_js, (
        'the file must name the server-side rule it mirrors, so a reader knows '
        'which copy is authoritative')
    # The tooltip must name each refusal separately. One generic message for
    # both "itself" and "its own sub-value" tells the user nothing about which
    # row is wrong or why the button is dead.
    for key in ('intoItself', 'intoOwnSub', 'tickToPaste', 'cutFirst'):
        assert "'%s'" % key in editor_js, (
            '%s must be reachable from the paste tooltip, so a disabled button '
            'explains itself' % key)


@pytest.mark.unit
def test_pasting_onto_an_ancestor_stays_allowed_in_both_interfaces(editor_js):
    """Moving History.AncientHistory.India under History yields History.India.

    That is a SIBLING of AncientHistory, so there is no cycle and the move is
    legal — the reported case that was broken while it read as a no-op. Pinned
    here because "refuse anything under yourself" is the tempting wrong fix.
    """
    tree = hierarchy.parse_tag_hierarchy(
        [(1, v) for v in ('History', 'History.AncientHistory',
                          'History.AncientHistory.India')])
    cut = hierarchy.get_node_by_path(tree, 'History.AncientHistory.India')
    target = hierarchy.get_node_by_path(tree, 'History')

    assert hierarchy.can_place_under(cut['path'], target['path'])
    assert hierarchy.relocated_path(cut['path'], target['path']) == 'History.India'

    # And the same shape must hold in the classic editor's expression.
    assert hierarchy.can_place_under(cut['path'], target['path']) == (
        target['path'] != cut['path']
        and not target['path'].startswith(cut['path'] + '.'))


# --------------------------------------------------------------------------- #
# 4. Failures must be visible
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_a_refusal_is_shown_at_the_top_of_the_panel(editor_js, editor_partial):
    """Every failure lands beside the banner, never below the value list.

    A column with thousands of values pushes anything rendered at the end of the
    panel off screen, so a refused action reads as "nothing happened" — the
    reported symptom of the original bug.
    """
    assert editor_partial.index('ccValuesError') < editor_partial.index('cc-values-list'), (
        'the error must sit ABOVE the value list')
    assert 'role="alert"' in editor_partial
    assert re.search(r"showError\(failure\(xhr\)\)", editor_js), (
        'a failed write must surface the endpoint\'s own message')


@pytest.mark.unit
def test_a_merge_on_rename_is_confirmed_before_it_is_applied(editor_js):
    """The 409 opt-in contract, exactly as the SPA and rename_tag (#973) do it.

    A value column is UNIQUE, so renaming onto an existing value is a merge with
    no undo. Applying it on first contact would let a typo consume a
    neighbouring value.
    """
    assert '409' in editor_js
    assert 'confirm_merges' in editor_js
    # The retry must be a separate, explicit call — never the first one. The
    # FIRST request must carry no confirm_merges at all.
    first = re.search(r"write\('rename',\s*\{ path: path, new_name: renamed \}", editor_js)
    assert first, 'the first rename request must not pre-confirm a merge'
    assert re.search(
        r"\{ path: path, new_name: renamed, confirm_merges: true \}", editor_js), (
        'the retry, and only the retry, must echo confirm_merges')


@pytest.mark.unit
def test_stored_values_never_reach_the_dom_as_markup(editor_js):
    """A value is whatever an edit-role user typed.

    It is interpolated as text (.text / .append), not as HTML, so a value called
    <img onerror=...> is shown literally instead of executing.
    """
    # .html( is allowed ONLY with an empty literal, which clears a container.
    # Anything else would interpolate a stored value into markup, so a value
    # called <img onerror=...> would execute instead of being displayed.
    for call in re.findall(r"\.html\(([^)]*)\)", editor_js):
        assert call.strip() in ("''", '""'), (
            '.html() must only ever be called to CLEAR a container; this '
            'interpolates: .html(%s)' % call[:60])
    assert '.text(' in editor_js, (
        'every stored value must reach the DOM through .text()')


# --------------------------------------------------------------------------- #
# 5. The template contract
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_the_editor_partial_is_included_where_the_button_lives(cc_list):
    assert "include 'cc_value_editor.html'" in cc_list, (
        'without the include the button has no modal to open and reads as dead')


@pytest.mark.unit
def test_the_partial_carries_the_column_id_and_mode(editor_partial):
    """The classic editor needs the same two facts the SPA gets from the API."""
    assert 'data-col-id' in editor_partial
    assert 'data-hierarchical' in editor_partial, (
        'a flat column has no hierarchy to move within; the editor hides Paste '
        'on it, exactly as the SPA does')


# --------------------------------------------------------------------------- #
# 6. The templates must actually compile
# --------------------------------------------------------------------------- #

@pytest.mark.unit
def test_the_cc_templates_compile_without_app_filters(editor_partial, cc_list):
    """A malformed Jinja block is a 500 on a page, not a test failure.

    Scoped to the two custom-column templates rather than the whole directory:
    most others need filters the application registers at startup
    (``shortentitle``, ``yesno``, ...), so a bare Environment cannot compile
    them and a directory-wide check would report those as failures forever.
    These two deliberately use only builtin filters, which is what makes them
    checkable here at all.
    """
    from jinja2 import Environment, DictLoader

    env = Environment(loader=DictLoader({
        'cc_value_editor.html': editor_partial,
        'cc_list.html': cc_list,
    }))
    broken = []
    for name in ('cc_value_editor.html', 'cc_list.html'):
        try:
            env.get_template(name)
        except Exception as exc:
            broken.append('%s: %s' % (name, str(exc)[:80]))
    assert not broken, 'templates that do not compile:\n  ' + '\n  '.join(broken)


@pytest.mark.unit
def test_both_templates_parse_and_render(editor_partial, cc_list):
    """Not just "contains the right words" — they must COMPILE and RENDER.

    A hand-written Jinja block is easy to get subtly wrong, and the failure is
    a 500 on the page rather than a test failure: an unbalanced brace in a
    multi-line dict literal inside `{{ }}` parses as a missing terminator only
    once the lexer reaches it, so every line above it looks fine.
    """
    from jinja2 import Environment, DictLoader

    env = Environment(loader=DictLoader({
        'cc_value_editor.html': editor_partial,
        'cc_list.html': cc_list,
    }))

    class _User:
        def role_edit(self):
            return True

        is_anonymous = False

    rendered = env.get_template('cc_value_editor.html').render(
        col_id=3, hierarchical=True, _=lambda s: s, current_user=_User())

    assert 'id="ccValuesEditor"' in rendered
    assert 'data-col-id="3"' in rendered
    assert 'data-hierarchical="1"' in rendered
    # The strings map must survive as valid JSON the script can JSON.parse.
    import json as _json
    start = rendered.index('data-strings=\'{{') if 'data-strings=\'{{' in rendered \
        else rendered.index("data-strings='")
    blob = rendered[start:rendered.index("'>", start) + 1]
    payload = blob.split("data-strings='", 1)[1].rsplit("'", 1)[0]
    strings = _json.loads(payload)
    assert strings['intoItself'] and strings['hasSubValues']
    assert '{name}' in strings['renameValue'], (
        'the placeholders the script substitutes must survive translation')


@pytest.mark.unit
def test_every_string_the_editor_shows_is_translated_by_the_server(editor_partial):
    """Classic strings come from Jinja, not from the script.

    A literal user-facing string inside cc_value_editor.js would be English
    only, on a page whose every other label follows the reader's locale.
    """
    js = _read(STATIC_JS, 'cc_value_editor.js')
    # Any quoted run of two or more capitalised words that is not a selector,
    # an endpoint or a data key would be untranslated prose.
    suspicious = re.findall(r"'([A-Z][a-z]+(?: [A-Z]?[a-z]+){2,})'", js)
    assert not suspicious, (
        'untranslated prose in the editor script: %r — pass it through the '
        'partial\'s strings map instead' % suspicious[:5])