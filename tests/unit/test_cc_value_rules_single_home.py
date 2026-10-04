# SPDX-License-Identifier: GPL-3.0-or-later
"""One home for the custom-column value rules — and proof of it.

Phases A–C moved the rename / move / merge / delete rules out of the API
endpoints and into :mod:`cps.hierarchy`, published the flags both interfaces
read, and gave the classic interface an editor that calls the same endpoints.
What was left unproven is the property that makes that worth anything: **the
rules have exactly one implementation.**

That is not a stylistic preference. The evidence is in this repository's own
history:

* ``ccPaste.ts`` grew a ``nothing-to-paste`` reason the server had no
  equivalent of, because the paste rules lived client-side.
* ``CcValueEditor.subtreePaths`` re-predicted the server's own
  ``subtree_values`` in TypeScript.
* The delete rule was briefly re-derived from ``children.length`` in three
  places at once, after having been published once.
* Invented glyphicon names (``clipboard``, ``compress``) rendered as BLANK
  buttons -- also invisible to the tests that existed at the time.

Each of those was a *silent* failure: nothing crashed, and nothing failed.
So these tests exist to make a reintroduced copy fail loudly instead.

Two halves:

1. :func:`test_a_rule_may_not_be_re_implemented` — a lint over the server tree
   and the frontend sources, with an explicit, justified allowlist.
2. :func:`test_the_cycle_rule_agrees_across_the_language_boundary` — one table
   of (path, destination) pairs, executed against BOTH implementations, in
   their own languages, from their own tests.
"""
import os
import re

import pytest

from cps import hierarchy

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
CPS = os.path.join(ROOT, 'cps')
FRONTEND_SRC = os.path.join(ROOT, 'frontend', 'src')

# The one file allowed to contain a rule. Everything else must call into it.
# Compared against _rel(), which normalises separators, so it must be a
# forward-slash path on every platform.
RULE_HOME = 'cps/hierarchy.py'


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, '/')


# Build output is not source. cps/static/app/ holds the Vite bundle, and the
# minified SPA legitimately contains the same expressions the lint looks for --
# a rewritten `children.length===0` in index-<hash>.js is not a second copy of
# the rule, it is a compiler artifact, and linting it produced a permanent,
# unactionable failure.
IGNORED_DIRS = ('node_modules', '__pycache__', 'app', 'libs', 'fonts')


def _source_files(root, extensions):
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for name in names:
            if name.endswith(extensions):
                yield os.path.join(base, name)


def _strip_noise(text):
    """Comments and string literals, so prose about a rule is not a copy of it.

    Both the explanation and the mirror matter: a docstring that *says* "a node
    may not be moved inside itself" is fine, and one that *computes* it is the
    bug. Only executable code can be the latter.
    """
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)      # /* block */
    text = re.sub(r'(?m)^\s*(//|#).*$', '', text)          # // and # line comments
    text = re.sub(r'"""(?:.|\n)*?"""', '""', text)         # Python docstrings
    text = re.sub(r"'''(?:.|\n)*?'''", "''", text)
    # Long strings are blanked; SHORT ones are kept, because the separator
    # literal '.' IS a one-character string. Blanking it made
    # startsWith(x + '.') invisible to the lint that exists to find exactly
    # that -- the self-test caught it, not the suite.
    text = re.sub(r"'[^']{4,}'", "''", text)
    text = re.sub(r'"[^"]{4,}"', '""', text)
    return text


# Each pattern is a way the cycle rule has been written by hand before.
#
# Both spellings of the separator count, because the two languages use
# different ones: TypeScript writes `cutPath + '.'` or names a SEPARATOR const,
# Python writes `x + SEPARATOR`. A lint that only knew one form would have
# called this suite green while the very file it exists to police went
# unexamined -- which is what happened on the first run.
# The operand before `+` is matched LOOSELY, because it is spelled several
# ways in practice: a bare name (`cutPath`), a module attribute
# (`hierarchy.SEPARATOR`), a member (`node.path`). A tighter `\w+` passed this
# lint on a violation written exactly the way the real code was -- the mutation
# probe caught it, which is the only reason this is loose now.
# NOTE ON SHAPE. These deliberately do NOT try to parse the whole expression.
# The first version spelled out the receiver, the operand and the separator
# literal, and the mutation probe proved it missed the real spelling
# (`t.startswith(p + hierarchy.SEPARATOR)`) for a reason that was not obvious
# from reading it -- an over-tight alternation silently failed to match, and the
# suite was green the whole time.
#
# So each pattern now says only what makes it a RULE COPY: a startsWith /
# indexOf / slice whose ARGUMENTS are built by concatenating a path with a
# separator. The receiver is `[\w.]{0,40}` -- loose on purpose. Being slightly
# over-broad costs a false positive someone deletes; being under-broad costs a
# duplicated rule that ships, which is what this whole phase exists to stop.
CYCLE_RULE_PATTERNS = [
    (re.compile(r"[Ss]tarts[Ww]ith\([^)]{0,40}\+[^)]{0,20}(?:SEPARATOR|'.'|\".\")"),
     "startsWith(<expr> + <separator>) -- the cycle rule"),
    (re.compile(r"[Ii]ndexOf\([^)]{0,40}\+[^)]{0,20}(?:SEPARATOR|'.'|\".\")\s*\)\s*[!=<>]=?\s*0"),
     "indexOf(<expr> + <separator>) === 0 -- the cycle rule"),
    (re.compile(r"[Ss]tarts[Ww]ith\([^)]{0,20}\+[^)]{0,20}'\.\)"),
     "startsWith(<expr> + '.') -- the cycle rule"),
    (re.compile(r"slice\(\s*0\s*,\s*-\s*1\s*\)\s*===?\s*[\w.]+\.length"),
     'the descendant test written as a slice'),
]

# The leaf rule: a children count deciding legality. The second is the merge
# wording, kept separate so the failure message names which operation drifted.
LEAF_RULE_PATTERNS = [
    (re.compile(r'children\.length\s*(?:===?|!==?)\s*0'),
     'children.length === 0 decides whether a value may be deleted'),
    (re.compile(r"children\?\.length\s*(?:===?|!==?)\s*0"),
     'children?.length === 0 decides whether a value may be deleted'),
    (re.compile(r"\['children'\]\.length\s*(?:===?|!==?)\s*0"),
     "children['length'] === 0 decides whether a value may be deleted"),
]

# split_path / join_path have been mirrored in TypeScript twice; those copies
# are allowlisted, but a FOURTH one appearing must not pass unnoticed.
#
# NOT included: a pattern for `can_delete`. Reading the published flag is the
# correct thing to do, and a lint cannot tell a read from a derivation -- the
# derivation IS the children count, which is already caught above.
CANONICALISE_PATTERNS = [
    (re.compile(r"split\(\s*['\"]\.['\"]\s*\)"),
     "a hand-written split('.') -- hierarchy.split_path/join_path own this"),
]

ALL_PATTERNS = CYCLE_RULE_PATTERNS + LEAF_RULE_PATTERNS + CANONICALISE_PATTERNS

# Frontend copies that are legitimate, each with the reason it is not a mirror.
#
# This allowlist is deliberately small and every entry is argued. A new entry
# should be treated as a design decision, not a convenience: the point of the
# lint is that adding one is uncomfortable.
ALLOWED_FRONTEND_COPIES = {
    'frontend/src/lib/ccPaste.ts': (
        'canPasteInto/pasteAvailability: the ONE rule that cannot be published. '
        'Whether a row may receive a moved value depends on the value the user '
        'has currently cut, which is client state the server never sees — so '
        'there is no per-node flag that could express it. Kept honest by '
        'test_the_cycle_rule_agrees_across_the_language_boundary, which runs '
        'this against hierarchy.can_place_under.'),
    'frontend/src/pages/CcBrowse.tsx': (
        'isAncestorPath: SELECTION state for the browse view, not an edit rule. '
        'It answers "is this row inside what the reader has open?" — a purely '
        'presentational question with no counterpart on the server, and it '
        'grants no permission and refuses nothing.'),
    'frontend/src/pages/CcValueEditor.tsx': (
        'rewritePath/subtreePaths: an OPTIMISTIC PREVIEW of what a rename would '
        'produce, so a collision can be shown before the request. The server '
        're-detects under its write lock and answers 409 regardless; the '
        'preview is never the decision. Labelled as predictive in the file.'),
    'frontend/src/components/CcValuePicker.tsx': (
        'canonicalize: the per-BOOK value picker, a different feature from the '
        'column-wide value editor. It writes the comma-joined selection into a '
        'form input and has no endpoints of its own.'),
    'frontend/src/components/Sidebar.tsx': (
        "isActive: NAVIGATION highlighting, not a value rule. It answers 'is "
        "this sidebar entry the one being viewed?' by comparing URL prefixes "
        "joined on '/'. Custom-column values are never joined on '/' -- their "
        "separator is '.', and a value may legitimately contain a slash "
        "('Photography.B/W'). Flagged only because the loose pattern cannot "
        "tell '+ separator' apart from '+ separator'."),
}


@pytest.mark.unit
def test_a_rule_may_not_be_re_implemented():
    """No hand-written copy of a value rule outside hierarchy.py.

    Server side this is absolute: the rules live in ``hierarchy.py``, so any
    ``p.startswith(x + '.')`` anywhere else in ``cps/`` is a new copy.
    """
    offenders = []

    for path in _source_files(CPS, ('.py',)):
        if _rel(path) == RULE_HOME:
            continue
        code = _strip_noise(_read(path))
        for pattern, label in ALL_PATTERNS:
            for match in pattern.finditer(code):
                line = code[:match.start()].count('\n') + 1
                offenders.append('%s:%d  %s' % (_rel(path), line, label))

    assert not offenders, (
        'a custom-column value rule was re-implemented outside %s. '
        'Call hierarchy.can_place_under / plan_move / plan_merge / plan_delete '
        'instead, or publish the verdict on the node:\n  %s'
        % (RULE_HOME, '\n  '.join(offenders)))


@pytest.mark.unit
def test_a_frontend_rule_may_not_be_re_implemented():
    """The same lint over the SPA, with a justified allowlist.

    Server-side duplication is a bug. Client-side duplication is only ever a
    bug when the client is DECIDING something the server also decides — so the
    allowlist is short, and each entry records why that copy is legitimate.
    """
    offenders, unexplained = [], []

    for path in _source_files(FRONTEND_SRC, ('.ts', '.tsx', '.js')):
        rel = _rel(path)
        code = _strip_noise(_read(path))
        hits = []
        for pattern, label in ALL_PATTERNS:
            for match in pattern.finditer(code):
                hits.append((label, code[:match.start()].count('\n') + 1))

        if not hits:
            continue
        if rel not in ALLOWED_FRONTEND_COPIES:
            unexplained.append('%s: %s' % (rel, ', '.join(h[0] for h in hits)))
            continue
        # Allowed, but say which rules it is allowed to carry, so the entry
        # cannot quietly widen into covering something new.
        offenders.append(
            '%s\n    allowed because: %s\n    carries: %s'
            % (rel, ALLOWED_FRONTEND_COPIES[rel],
               ', '.join('%s (line %d)' % (h[0], h[1]) for h in hits)))

    assert not unexplained, (
        'a value rule was re-implemented in the SPA with no justification:\n  %s\n'
        'If it is a legitimate copy, add it to ALLOWED_FRONTEND_COPIES with a '
        'reason; if it is a decision the server should be making, call the '
        'published flag instead.' % '\n  '.join(unexplained))

    # Not an assertion about content -- a reminder that the allowlist is not
    # allowed to rot into a blank cheque.
    for entry in offenders:
        assert 'allowed because' in entry


@pytest.mark.unit
def test_the_allowlist_is_not_stale():
    """Every allowlisted file must still contain the copy that justified it.

    A stale entry is worse than none: it silently permits whatever is written
    next, in a file nobody is watching.
    """
    stale = []
    for rel in ALLOWED_FRONTEND_COPIES:
        path = os.path.join(ROOT, *rel.split('/'))
        if not os.path.exists(path):
            stale.append('%s  (file is gone -- drop the entry)' % rel)
            continue
        # Against the RAW source, not the stripped one: the patterns look for
        # a separator LITERAL ("." or SEPARATOR), and _strip_noise deliberately
        # blanks string literals -- so running them on stripped code made every
        # entry look stale. This lint is about what the file still contains,
        # and prose is harmless here.
        raw = _read(path)
        if not any(p.search(raw) for p, _ in ALL_PATTERNS):
            stale.append('%s  (no longer contains the copy -- drop the entry)' % rel)

    assert not stale, 'ALLOWED_FRONTEND_COPIES has gone stale:\n  ' + '\n  '.join(stale)


@pytest.mark.unit
def test_the_lint_itself_catches_a_reintroduced_copy(tmp_path):
    """The lint must fail on a violation, or it is decoration.

    Two earlier versions of this suite passed on a deliberate violation:

    * the cycle patterns matched only a bare ``\\w+`` operand, so the copy
      written as ``hierarchy.SEPARATOR`` -- exactly how the real code spells it
      -- slipped through;
    * the leaf pattern ignored the minified bundle, which was the only thing it
      actually fired on, so it had never been shown to catch anything.

    A lint that cannot be shown to fail has not been tested.
    """
    violations = [
        # Python, inside cps/ -- the copy that the narrow pattern missed.
        ('def guard(p, t):\n    return t.startswith(p + hierarchy.SEPARATOR)\n',
         'cps/zz_probe.py'),
        # TypeScript, inside frontend/src/ -- the leaf rule re-derived.
        ('export function leaf(n) {\n  return n.children.length === 0;\n}\n',
         'frontend/src/zzprobe.ts'),
        # The cycle rule in its other TypeScript spelling, via a named constant.
        ("export function cyc(c, t) {\n  return t.startsWith(c + '.');\n}\n",
         'frontend/src/zzprobe2.ts'),
    ]

    for source, relative in violations:
        target = os.path.join(ROOT, *relative.split('/'))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as handle:
            handle.write(source)
        try:
            code = _strip_noise(_read(target))
            matched = [label for pattern, label in ALL_PATTERNS
                       if pattern.search(code)]
            assert matched, (
                'the lint did NOT flag this copy in %s:\n%s\n'
                'A lint that cannot be shown to fail has not been tested.'
                % (relative, source))
        finally:
            os.remove(target)
            parent = os.path.dirname(target)
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)


# --------------------------------------------------------------------------- #
# The cross-language table
# --------------------------------------------------------------------------- #

# ONE table, in ONE file, read by BOTH implementations. It lives in
# frontend/unit/ccCycleCases.json because the TypeScript half has to import it
# and JSON is the only thing both runners can read; the Python half loads it by
# path. Neither language owns a copy, which is what stops them diverging.
CYCLE_TABLE = os.path.join(ROOT, 'frontend', 'unit', 'ccCycleCases.json')


def _cycle_cases():
    import json
    with open(CYCLE_TABLE, encoding='utf-8') as handle:
        return json.load(handle)['cases']


@pytest.mark.unit
@pytest.mark.parametrize('case', _cycle_cases(),
                         ids=[c['name'] for c in _cycle_cases()])
def test_the_cycle_rule_agrees_across_the_language_boundary(case):
    """hierarchy.can_place_under, on the table the TypeScript half also runs.

    This is the only check that can catch the drift the allowlist above
    deliberately permits: canPasteInto is a real second implementation of
    can_place_under, because the rule needs the value the user has cut and no
    per-node flag could carry that. Its counterpart is
    ``frontend/unit/ccValuePasteParity.test.ts``, which runs the same JSON
    against the TypeScript.
    """
    assert hierarchy.can_place_under(case['cut'], case['onto']) is case['allowed'], (
        '%s -- cut %r onto %r'
        % (case.get('why', case['name']), case['cut'] or '<none>',
           case['onto'] or '<root>'))


@pytest.mark.unit
def test_both_implementations_read_the_same_table():
    """Neither side may hold a private copy of the cases.

    If either test file spelled its cases inline, a case added to the other
    would be silently invisible -- which is the failure mode this whole file
    exists to prevent, reintroduced one layer up.
    """
    assert os.path.exists(CYCLE_TABLE), (
        'the shared cycle table must exist; both implementations read it')
    ts_test = os.path.join(ROOT, 'frontend', 'unit', 'ccValuePasteParity.test.ts')
    with open(ts_test, encoding='utf-8') as handle:
        source = handle.read()
    assert 'ccCycleCases.json' in source, (
        'the TypeScript half must read the shared table, not spell its own')


@pytest.mark.unit
def test_the_table_covers_the_cases_that_matter():
    """The table must keep its awkward members, not just its easy ones.

    A table of obvious cases would pass forever while the one interesting
    disagreement -- a shared prefix that is not a path segment -- went
    unnoticed. These three are the ones that have actually been got wrong.
    """
    cases = _cycle_cases()

    def has(cut, onto):
        return any(c['cut'] == cut and c['onto'] == onto for c in cases)

    assert len(cases) >= 8, (
        'the table shrank to %d cases; both implementations would then be '
        'untested on the awkward inputs' % len(cases))
    assert has('History.AncientHistory.India', 'History'), (
        'the reported case: pasting onto an own ancestor is ALLOWED')
    assert has('TestNode', 'TestNodeOther'), (
        'a shared prefix is not a path segment, so this must be allowed')
    assert has('TestNode', 'TestNode.TestSubNode.TestDeepNode'), (
        'the cycle guard must hold at depth, not just one level')


# --------------------------------------------------------------------------- #
# Interface parity
# --------------------------------------------------------------------------- #

@pytest.fixture(scope='module')
def editor_js():
    """The classic editor's script, for the interface-parity checks below."""
    return _read(os.path.join(ROOT, 'cps', 'static', 'js', 'cc_value_editor.js'))


# The five operations, and what each interface's control is called. These are
# the two renderers of the SAME tree: the classic partial renders its rows
# server-side and lets cc_value_editor.js attach the actions, while the SPA
# builds its rows in CcValueEditor.tsx.
# The five operations. `icon` is the glyphicon NAME, because that is how each
# interface actually identifies its control: the classic editor passes it to
# action(), the SPA names the glyphicon class. `merge` is 'transfer' -- there is
# no 'compress' or 'clipboard' glyphicon in this Bootstrap, which is why two of
# these buttons once rendered blank.
THE_FIVE_ACTIONS = {
    'rename': ('pencil', 'Pencil'),
    'cut': ('scissors', 'Scissors'),
    'paste': ('paste', 'ClipboardPaste'),
    'merge': ('transfer', 'Merge'),
    'delete': ('trash', 'Trash2'),
}


@pytest.mark.unit
def test_both_interfaces_offer_the_same_five_operations(editor_js):
    """Same operations, same icons, on both interfaces.

    Parity that is structural rather than maintained by hand: both UIs read the
    same `available` flags and call the same five endpoints, so the set of
    ACTIONS is the thing that can still drift -- a button added to one and not
    the other. Cheap to assert, and it is the difference the phases were for.
    """
    spa = _read(os.path.join(ROOT, 'frontend', 'src', 'pages', 'CcValueEditor.tsx'))
    for name, (icon, component) in THE_FIVE_ACTIONS.items():
        assert "action('%s'" % icon in editor_js, (
            'the classic editor lost its %s control (icon %s)' % (name, icon))
        assert '<%s' % component in spa, (
            'the SPA lost its %s control (%s) — the two interfaces must offer '
            'the same operations' % (name, component))


@pytest.mark.unit
def test_neither_interface_derives_the_enabled_state_itself(editor_js):
    """Both must read the published flag, not re-derive it.

    The classic editor reads data-can-delete / data-can-merge-source; the SPA
    reads available.can_delete / available.can_merge_source. Different
    transports, ONE decision -- hierarchy.node_availability.
    """
    spa = _read(os.path.join(ROOT, 'frontend', 'src', 'pages', 'CcValueEditor.tsx'))
    assert "data('can-delete')" in editor_js
    assert 'available.can_delete' in spa, (
        'the SPA must read the published flag, not children.length')
    assert "data('can-merge-source')" in editor_js
    # The SPA wraps the flag in a single callback (canBeSource) so the row
    # action list stays readable, so the literal appears once, not per use.
    assert 'available?.can_merge_source' in spa, (
        'the SPA must read the published merge-source flag, not children.length')
    assert 'canBeSource' in spa, (
        'and it must be consulted for every source, not only the first')


@pytest.mark.unit
def test_move_is_hidden_on_a_flat_column_in_both_interfaces(editor_js):
    """A flat column has no hierarchy, so there is nothing to move within.

    Both UIs must agree on this, and it is the one place where a disagreement
    is visible to the user as a button that is there and does nothing.
    """
    spa = _read(os.path.join(ROOT, 'frontend', 'src', 'pages', 'CcValueEditor.tsx'))
    assert "hierarchical ? action('paste'" in editor_js, (
        'the classic editor must hide Paste on a flat column')
    assert '{hierarchical && (' in spa, (
        'the SPA must hide its Paste control on a flat column too')