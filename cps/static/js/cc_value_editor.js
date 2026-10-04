/* Custom-column VALUE editor for the classic interface.
 *
 * The twin of the new interface's CcValueEditor, on /custom_column/<id>. Both
 * drive the SAME five endpoints (/api/v1/columns/<id>/values/*), so there is
 * one implementation of every operation and one set of permission checks.
 *
 * IT EDITS IN PLACE. The panel is already in the page markup, rendered by
 * cc_value_editor.html from the tree web.py already had in hand; this file only
 * swaps which of the two views is visible. A modal was tried first and was
 * wrong on both counts the users noticed: it covered the tree being edited,
 * and it was a small box on a screen with plenty of room.
 *
 * WHAT IT DOES NOT DO: decide which values may be deleted or used as a merge
 * source. Those arrive as `data-can-delete` / `data-can-merge-source`, computed
 * by hierarchy.node_availability. Before that flag existed this file would have
 * re-derived them, which is precisely how two interfaces drift apart.
 *
 * WHAT IT MUST DO, because it is selection state the server cannot see:
 *
 *   1. SELECTION. Cut and Paste name one value and one destination, and Merge
 *      names a destination plus its sources. None is safe to trigger on a row
 *      the user merely scrolled past, so each is offered only on a ticked row.
 *   2. NO CYCLES. A value must not become its own ancestor. This is the ONE
 *      rule that still has a client copy, because it needs the held value:
 *
 *          canPasteInto(cutPath, targetPath)  ===  hierarchy.can_place_under
 *
 *      Note what is NOT refused: pasting into an ANCESTOR. Moving
 *      History.AncientHistory.India under History yields History.India, a
 *      SIBLING of AncientHistory — no cycle. Only the node itself and its own
 *      descendants are. The server refuses both cases regardless, because
 *      another session may have changed the tree since this page was rendered.
 *
 * CSRF and the reverse-proxy mount prefix come from the same helpers every
 * other classic call uses: getPath() and the csrf_token input layout.html
 * already renders.
 */
/* global jQuery, getPath */
(function ($) {
    'use strict';

    $(function () {
        var $editor = $('#ccValuesEditor');
        if (!$editor.length) {
            return;                       // this account may not edit values
        }

        var colId = $editor.data('col-id');
        var hierarchical = String($editor.data('hierarchical')) === '1';
        var strings = $editor.data('strings') || {};

        var $browse = $('.cc-values-browse');
        var $rows = $editor.find('.cc-values-row');
        /* The one control that toggles the mode. Hoisted because setMode rewrites
         * its CLASSES, and a binding scoped to a state class cannot survive that.
         * cc_list.html renders the button carrying only the Edit class, so binding
         * the Done state class separately matched NOTHING at boot and attached a
         * handler to an empty set; the class only appeared later, after the
         * binding had already been made against zero elements, so Done did nothing
         * at all. Binding the stable slot instead, and branching on its current
         * class, makes the class swap irrelevant to wiring. */
        var $slot = $('.cc-values-toolbar .cc-values-toggle');
        var cutPath = null;
        var busy = false;

        // ------------------------------------------------------------- helpers

        function t(key, vars) {
            var text = strings[key] || key;
            Object.keys(vars || {}).forEach(function (name) {
                text = text.split('{' + name + '}').join(vars[name]);
            });
            return text;
        }

        function csrf() {
            return { 'X-CSRFToken': $("input[name='csrf_token']").val() };
        }

        /* The ONE cycle rule. Mirrors cps/hierarchy.py::can_place_under. A
         * path-SEGMENT prefix, not a bare string prefix: 'Historyics' merely
         * starts with 'History' and is a sibling, not a child. */
        function canPasteInto(cut, target) {
            if (!cut) {
                return false;
            }
            return target !== cut && String(target).indexOf(cut + '.') !== 0;
        }

        function rowFor(path) {
            return $rows.filter(function () { return $(this).data('path') === path; });
        }

        function nameOf(path) {
            var $row = rowFor(path);
            return $rows.length ? $row.find('.cc-values-name').text() : path;
        }

        function isChecked(path) {
            return rowFor(path).find('.cc-values-tick').prop('checked') === true;
        }

        function chosen() {
            return $rows.filter(function () {
                return $(this).find('.cc-values-tick').prop('checked');
            }).map(function () {
                return $(this).data('path');
            }).get();
        }

        /* The destination is the row the button was pressed on, so it is never
         * among its own sources. Mirrors hierarchy.merge_sources. */
        function mergeSources(destination) {
            return chosen().filter(function (p) { return p !== destination; });
        }

        function showError(message) {
            $('#ccValuesError').text(message || '').prop('hidden', !message);
        }

        function banner(message) {
            $('#ccValuesBanner').text(message || '').prop('hidden', !message);
        }

        function setBusy(value) {
            busy = value;
            $editor.find('button').prop('disabled', value);
        }

        /* Whether edit mode is on, remembered across the reload a write causes.

         * Every write re-reads the page, because a rename rewrites every stored
         * spelling beneath it and patching the DOM would leave the browser
         * asserting facts the database no longer holds. But the reload used to
         * drop the user back into the browse view, so renaming one value in a
         * column of a hundred meant clicking Edit values again for each -- and
         * losing sight of which one they were on.

         * sessionStorage, keyed per column, for the same reason hierarchy.js
         * stores its expand state there: it survives a reload, is scoped to the
         * tab, and needs no server round trip. */
        var modeKey = 'cw-cc-values-edit-' + colId;

        function rememberMode(editing) {
            try {
                if (editing) {
                    sessionStorage.setItem(modeKey, '1');
                } else {
                    sessionStorage.removeItem(modeKey);
                }
            } catch (e) { /* storage unavailable (private mode etc.) */ }
        }

        function wasEditing() {
            try {
                return sessionStorage.getItem(modeKey) === '1';
            } catch (e) {
                return false;
            }
        }

        /* Reload after a write so the server re-renders the tree from the data
         * it just committed. The edit-mode flag is re-applied by the boot code
         * below, which is why the mode survives. */
        function reload() {
            window.location.reload();
        }

        // ------------------------------------------------------------ requests

        /* `onConflict` handles the 409 opt-in: a rename that would land on an
         * existing value is a merge, and is never applied on first contact. */
        function write(action, body, onConflict) {
            if (busy) {
                return;
            }
            setBusy(true);
            showError('');
            $.ajax({
                url: getPath() + '/api/v1/columns/' + colId + '/values/' + action,
                method: 'POST',
                contentType: 'application/json; charset=utf-8',
                headers: csrf(),
                data: JSON.stringify(body)
            }).done(function () {
                reload();
            }).fail(function (xhr) {
                setBusy(false);
                if (xhr.status === 409 && onConflict) {
                    onConflict(xhr.responseJSON);
                    return;
                }
                showError(failure(xhr));
            });
        }

        /* A status code is a better message than the raw body for the codes
         * these endpoints actually return, because each was written for exactly
         * this situation. */
        function failure(xhr) {
            var body = (xhr.responseJSON && xhr.responseJSON.error) || {};
            var byStatus = {
                400: t('nameInUse'),
                401: t('signIn'),
                403: t('notAllowed'),
                404: t('notFound')
            };
            return byStatus[xhr.status] || body.message || 'Error';
        }

        // --------------------------------------------------------------- render

        /* Row actions are built here rather than server-rendered because their
         * enabled state depends on the live selection. The ROWS themselves come
         * from the server, flags and all. */
        function render() {
            $rows.each(function () {
                var $row = $(this);
                var path = $row.data('path');
                var canDelete = $row.data('can-delete') === 1 || $row.data('can-delete') === '1';
                var canBeSource = $row.data('can-merge-source') === 1
                    || $row.data('can-merge-source') === '1';
                var name = $row.find('.cc-values-name').text();
                var sources = mergeSources(path);
                var pasteOk = hierarchical && canPasteInto(cutPath, path) && isChecked(path);
                var canMerge = isChecked(path) && sources.length > 0 && sources.every(function (p) {
                    var $other = rowFor(p);
                    return $other.data('can-merge-source') === 1
                        || $other.data('can-merge-source') === '1';
                });

                $row.toggleClass('cc-values-cut', cutPath === path);
                // Marked from the cycle rule alone, so the rows that CAN receive
                // the held value are visible before any of them is ticked.
                $row.toggleClass('cc-values-target',
                    hierarchical && canPasteInto(cutPath, path));

                $row.find('.cc-values-actions').empty().append([
                    action('pencil', t('renameValue', { name: name }), t('rename'),
                        function () { runRename(path, name); }, false),
                    action('scissors', t('cutValue', { name: name }),
                        isChecked(path) ? t('cut') : t('tickToCut'),
                        function () { cutPath = path; showError(''); render(); },
                        !isChecked(path)),
                    // Icons verified against the shipped Bootstrap 3 build:
                    // `clipboard` and `compress` DO NOT EXIST there, so those
                    // buttons rendered blank. `paste` and `transfer` do.
                    hierarchical ? action('paste', t('pasteUnder', { name: name }),
                        pasteTitle(path), function () { runMove(path); }, !pasteOk) : null,
                    action('transfer', t('mergeSelectedInto', { name: name }),
                        mergeTitle(path, sources), function () { runMerge(path, sources); },
                        !canMerge),
                    action('trash', t('deleteValue', { name: name }),
                        canDelete ? t('delete') : t('deleteNeedsLeaf'),
                        function () { runDelete(path); }, !canDelete)
                ].filter(Boolean));
            });

            banner(cutPath ? t('readyToPaste', { name: cutPath }) : '');
        }

        /* A disabled control with no explanation is what users report as
         * "broken", so every blocked state gets its own sentence. */
        function action(icon, label, title, onClick, disabled) {
            return $('<button type="button" class="btn btn-default btn-xs"></button>')
                .attr('title', title)
                .attr('aria-label', label)
                .prop('disabled', !!disabled || busy)
                .append($('<span class="glyphicon"></span>').addClass('glyphicon-' + icon))
                .on('click', onClick);
        }

        /* Why Paste is disabled. Two independent gates, so the user can tell
         * whether to tick the row or choose another destination. */
        function pasteTitle(path) {
            if (!cutPath) { return t('cutFirst'); }
            if (!isChecked(path)) { return t('tickToPaste'); }
            if (path === cutPath) { return t('intoItself'); }
            if (String(path).indexOf(cutPath + '.') === 0) { return t('intoOwnSub'); }
            return t('paste');
        }

        function mergeTitle(path, sources) {
            if (!isChecked(path)) { return t('tickToMerge'); }
            if (sources.length === 0) { return t('tickAnother'); }
            var offending = sources.filter(function (p) {
                var $other = rowFor(p);
                return !($other.data('can-merge-source') === 1
                    || $other.data('can-merge-source') === '1');
            })[0];
            return offending ? t('sourceHasChildren', { name: offending })
                             : t('mergeInto', { name: nameOf(path) });
        }

        // --------------------------------------------------------------- writes

        function runMove(newParent) {
            var moving = cutPath;
            if (!window.confirm(t('mergeConfirm', { name: moving }))) {
                return;
            }
            write('move', { path: moving, new_parent: newParent || '' });
        }

        function runMerge(into, sources) {
            if (!sources.length) { return; }
            if (!window.confirm(t('mergeConfirm', { name: nameOf(into) }))) {
                return;
            }
            write('merge', { paths: sources, into: into });
        }

        function runDelete(path) {
            if (!window.confirm(t('deleteConfirm', { name: nameOf(path) }))) {
                return;
            }
            write('delete', { paths: [path] });
        }

        function runCreate() {
            var value = window.prompt(t('createPrompt'));
            if (!value || !value.trim()) { return; }
            write('create', { path: value.trim() });
        }

        /* A rename that would land on an existing value is a merge, and there
         * is no undo — so the 409 is shown as a question and the request is
         * repeated with confirm_merges. The first request must not pre-confirm. */
        function runRename(path, name) {
            var draft = window.prompt(t('renamePrompt', { name: name }), name);
            if (!draft || !draft.trim() || draft.trim() === name) { return; }
            var renamed = draft.trim();
            write('rename', { path: path, new_name: renamed }, function () {
                if (window.confirm(t('renameMergeConfirm'))) {
                    write('rename',
                        { path: path, new_name: renamed, confirm_merges: true });
                }
            });
        }

        // ----------------------------------------------------------------- boot

        $editor.on('click', 'input.cc-values-tick', function () {
            // A tick changes which rows are legal destinations, so the actions
            // are rebuilt. `render` reads the checkbox, which is already
            // updated by the time this delegated handler runs.
            render();
        });

        /* Edit values and Done occupy the SAME right-hand toolbar slot and swap
         * in place, rather than one replacing the other at different positions.
         * A control that moves when the mode changes is read as a layout jump,
         * and the user has to hunt for it again. */
        function setMode(editing) {
            $editor.prop('hidden', !editing);
            $browse.prop('hidden', editing);
            cutPath = editing ? cutPath : null;
            if (!editing) {
                showError('');
            }
            $slot.toggleClass('btn-cc-values-edit', !editing)
                .toggleClass('btn-cc-values-done', editing)
                .prop('title', editing ? t('done') : t('editValues'))
                .text(editing ? t('done') : t('editValues'));
            rememberMode(editing);
            if (editing) {
                render();
            }
        }

        $('#ccValuesCreate').on('click', runCreate);

        /* One handler for both directions, on the element that exists in both
         * modes. Which direction is decided by the class setMode just applied, so
         * the two buttons need no separate bindings. */
        $slot.on('click', function () {
            if ($slot.hasClass('btn-cc-values-done')) {
                setMode(false);
                // The same element is the Edit button again, so focus lands back
                // on the control the user is about to press next.
                $slot.trigger('focus');
            } else {
                setMode(true);
                $editor.find('input.cc-values-tick').first().trigger('focus');
            }
        });

        /* Restore the mode a write's reload interrupted. Only when the user
         * actually left edit mode deliberately is the flag cleared, so an
         * unrelated visit to this page later does not drop them into a
         * metadata write panel they never asked for. */
        if (wasEditing()) {
            setMode(true);
        }
    });
}(jQuery));