import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ClipboardPaste, Merge, Pencil, Plus, Scissors, Trash2, X } from 'lucide-react';
import { useCcTree, useCreateCcValue, useDeleteCcValues, useMergeCcValues, useMoveCcValue, useRenameCcValue } from '../lib/queries';
import type { CcNode, CcValueConflict } from '../lib/api';
import { ApiError } from '../lib/api';
import { useT } from '../lib/i18n';
import { canCut, canPasteInto, isValidPasteTarget, pasteAvailability } from '../lib/ccPaste';
import type { PasteBlockReason } from '../lib/ccPaste';
import { mergeAvailability, mergeSourcePaths } from '../lib/ccMerge';
import { useAnnouncer } from '../lib/a11y/announcer';
import styles from './CcValueEditor.module.css';

/** Flatten the tree to [{node, depth}], DFS order — the order the tree is shown in. */
function flatten(nodes: CcNode[], depth = 0, out: { node: CcNode; depth: number }[] = []) {
  for (const node of nodes) {
    out.push({ node, depth });
    flatten(node.children ?? [], depth + 1, out);
  }
  return out;
}

/** Every value in the subtree rooted at `root`, using canonical dotted paths.
 *
 *  The server rewrites the stored *spellings* (which may differ from the
 *  canonical path, e.g. 'Arts.' vs 'Arts'), so this predicts the node paths the
 *  server will produce — enough to spot a collision before the request. */
function subtreePaths(root: CcNode): string[] {
  const paths = [root.path];
  for (const child of root.children ?? []) paths.push(...subtreePaths(child));
  return paths;
}

/** Where a rename would land, given the node's canonical path and the typed leaf
 *  name. Mirrors cps/hierarchy.py join_path: segments stripped, empties dropped. */
function rewritePath(current: string, newName: string): string {
  const leaf = current.split('.').pop() ?? current;
  const parent = current.split('.').slice(0, -1);
  return [...parent, ...newName.split('.')].map((p) => p.trim()).filter(Boolean).join('.') || leaf;
}

type Mode = 'idle' | 'rename' | 'mergeInto' | 'delete' | 'create';

/** How far each level of nesting shifts right, and the gutter a root row keeps.
 *
 *  Was 16px/level, which read as a flat list once a column was three or four
 *  levels deep -- the nesting was there but invisible. 40px makes the depth
 *  obvious at a glance.
 *
 *  Mirrored by `indent_px` in cps/templates/cc_value_editor.html. The classic and
 *  new interfaces render the SAME tree, so a difference here would make the same
 *  hierarchy look like two different depths depending on where you opened it. */
const INDENT_PX = 40;
const INDENT_ROOT_PX = 8;

/** Read the merge list off a 409, or null if this was not a collision.
 *  Mirrors `tagConflictOf`: the body rides on `ApiError.detail`. */
function ccConflictsOf(error: unknown): CcValueConflict[] | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null;
  const list = error.detail?.conflicts as CcValueConflict[] | undefined;
  return Array.isArray(list) && list.length ? list : null;
}

interface CcValueEditorProps {
  colId: string;
  /** Whether values are dotted-hierarchical; flat columns hide Move. */
  hierarchical: boolean;
  onDone: () => void;
}

/** Edit mode for one custom column's values.
 *
 *  The whole tree is already in memory (useCcTree), so the collision check for a
 *  cascading rename runs with no extra request: apply the same prefix rewrite the
 *  server will do and intersect against every existing path.
 *
 *  That check is a WARNING, not a guard. The cached tree can be 60s stale and
 *  another session can create the same collision in between, so the server
 *  re-detects under its write lock and answers 409 unless `confirm_merges` is
 *  true — this component just echoes the flag on the retry, which is the same
 *  opt-in flow the tag editor uses.
 */
export function CcValueEditor({ colId, hierarchical, onDone }: CcValueEditorProps) {
  const t = useT();
  const announce = useAnnouncer();
  const { data: tree } = useCcTree(colId);
  const rename = useRenameCcValue(colId);
  const merge = useMergeCcValues(colId);
  const remove = useDeleteCcValues(colId);
  const move = useMoveCcValue(colId);
  const create = useCreateCcValue(colId);

  const [mode, setMode] = useState<Mode>('idle');
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  // The single value rename/merge-destination acts on. Captured when the button
  // is pressed, because the panel no longer has the checkbox selection to read
  // it from.
  const [target, setTarget] = useState<string>('');
  // The values a Delete confirmation covers. Separate from `chosen` because an
  // inline Delete on a row must act on THAT value, whatever else is ticked --
  // deleting a neighbour the user never looked at would be a nasty surprise.
  const [deleteTargets, setDeleteTargets] = useState<string[]>([]);
  // The value waiting to be pasted somewhere else (a relocation buffer).
  // Null when nothing is held. Never persisted: leaving edit mode drops it.
  const [cutPath, setCutPath] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [err, setErr] = useState('');
  const [conflicts, setConflicts] = useState<CcValueConflict[] | null>(null);

  const rows = useMemo(() => flatten(tree?.nodes ?? []), [tree]);
  const chosen = useMemo(() => Object.keys(checked).filter((k) => checked[k]), [checked]);
  const nodeByPath = useMemo(() => {
    const map = new Map<string, CcNode>();
    for (const { node } of rows) map.set(node.path, node);
    return map;
  }, [rows]);
  // Merge folds every OTHER ticked value into the row it is clicked on. The row
  // itself must be ticked too, so the button only appears on values the user has
  // actually chosen as a destination. ccMerge owns the selection rules; this only
  // reads them, so the button state and the request cannot drift apart.
  const mergeSources = useMemo(() => mergeSourcePaths(chosen, target), [chosen, target]);
  // Whether a value may be a merge SOURCE is decided server-side and published on
  // the node, rather than re-derived here from `children.length` — that derivation
  // was a second copy of a rule the server already owns. The `??` fallback keeps
  // the SPA working against a server that predates the field.
  const canBeSource = useCallback(
    (p: string) => nodeByPath.get(p)?.available?.can_merge_source
      ?? (nodeByPath.get(p)?.children.length ?? 0) === 0, [nodeByPath]);

  // Entering a panel clears the transient error/conflict state. The selection is
  // deliberately NOT cleared: clearing it made `single` null the moment rename
  // opened, which disabled Save with no way back short of cancelling.
  useEffect(() => { setErr(''); setConflicts(null); }, [mode]);

  // Escape abandons a held cut, and otherwise leaves edit mode entirely. Without
  // it a cut could only be cleared by finding the banner's Cancel.
  useEffect(() => {
    if (mode !== 'idle') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      // A specific string, not a bare "Cancelled": the latter fuzzy-matches an
      // existing msgid and the anchored-msgid guard rejects a fuzzy translation.
      if (cutPath) { setCutPath(null); announce(t('Paste cancelled.')); return; }
      onDone();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [mode, cutPath, onDone, announce]);

  const failure = (error: unknown, fallback: string) => {
    if (error instanceof ApiError) {
      const messages: Record<number, string> = {
        400: t('That name cannot be used'),
        401: t('You must be signed in'),
        403: t('You are not allowed to edit metadata'),
        404: t('Value not found'),
      };
      return messages[error.status] ?? fallback;
    }
    return fallback;
  };

  const toggle = (p: string) => setChecked((prev) => ({ ...prev, [p]: !prev[p] }));
  const reset = () => {
    setMode('idle'); setDraft(''); setErr(''); setConflicts(null);
    setChecked({}); setDeleteTargets([]);
  };

  /** Which values a rename of `target` would merge, computed from the tree we
   *  already hold. Empty when the rename is a plain rename. */
  const predictedMerges = useMemo(() => {
    if (!target || mode !== 'rename' || !tree) return [] as string[];
    const root = nodeByPath.get(target);
    if (!root) return [];
    const taken = new Set<string>();
    const collect = (nodes: CcNode[]) => { for (const n of nodes) { taken.add(n.path); collect(n.children ?? []); } };
    collect(tree.nodes);
    const out: string[] = [];
    for (const p of subtreePaths(root)) {
      const next = rewritePath(p, draft.trim());
      if (next !== p && taken.has(next)) out.push(next);
    }
    return out;
  }, [target, mode, draft, tree, nodeByPath]);

  /** Why the Merge button on `path` is (or is not) available right now.
   *
   *  A disabled control with no explanation is what users report as "broken",
   *  so each blocked state gets its own sentence. */
  const mergeTitle = (path: string): string => {
    const { ok, reason, offendingSource } = mergeAvailability(chosen, path, canBeSource);
    if (ok) return t('Merge the other selected values into {name}', { name: nodeByPath.get(path)?.name ?? path });
    if (reason === 'not-ticked') return t('Tick this value to merge the others into it');
    if (reason === 'needs-another') return t('Tick another value to merge into this one');
    // Name the actual offender: with several values ticked, "only leaves can be
    // merged" gives the user no idea WHICH one to untick.
    return t('“{name}” has sub-values, so it cannot be merged away. Move or rename it instead.',
      { name: offendingSource ?? '' });
  };

  /** Why the Paste button on `path` is disabled, for its tooltip.
   *
   *  Two gates can block it and they are independent -- the user needs to be told
   *  which one, or they cannot tell whether to tick the row or to choose another
   *  destination. */
  const pasteReason = (reason: PasteBlockReason): string => {
    if (reason === 'not-ticked') return t('Tick this value to paste into it');
    if (reason === 'nothing-to-paste') return t('Cut a value first, then choose where it should go');
    if (reason === 'into-itself') return t('A value cannot be pasted into itself');
    if (reason === 'into-descendant') {
      return t('A value cannot be pasted into one of its own sub-values');
    }
    return t('Paste');
  };

  /** Paste the held value under `ontoPath` ('' = the top level). */
  const runPaste = (ontoPath: string) => {
    if (!cutPath || !canPasteInto(cutPath, ontoPath)) return;
    const label = cutPath;
    move.mutate(
      { path: cutPath, newParent: ontoPath },
      {
        onSuccess: () => {
          setCutPath(null);
          announce(ontoPath
            ? t('Moved {name} under {parent}', { name: label, parent: ontoPath })
            : t('Moved {name} to the top level', { name: label }));
          setErr('');
        },
        onError: (error) => {
          if (ccConflictsOf(error)) { setErr(t('Something is already there. Move it first.')); return; }
          setErr(failure(error, t('Could not move')));
        },
      },
    );
  };

  const runRename = (confirmMerges: boolean) => {
    if (!target) return;
    rename.mutate(
      { path: target, newName: draft.trim(), confirmMerges },
      {
        onSuccess: (r) => {
          announce(r.merged?.length
            ? t('Renamed and merged {count} values', { count: r.merged.length })
            : t('Renamed {name}', { name: target }));
          reset();
        },
        onError: (error) => {
          const found = ccConflictsOf(error);
          if (found && !confirmMerges) { setConflicts(found); setErr(''); return; }
          setConflicts(null);
          setErr(failure(error, t('Could not rename')));
        },
      },
    );
  };


  const busy = rename.isPending || merge.isPending || remove.isPending || move.isPending || create.isPending;

  // ------------------------------------------------------------------ panels
  if (mode === 'rename' || mode === 'create') {
    const title = mode === 'rename' ? t('Rename value') : t('Create value');
    const label = mode === 'rename' ? t('New name') : t('New value');
    const placeholder = mode === 'create' ? 'Arts.Mystery' : 'Culture';
    const canSubmit = mode === 'create' ? !!draft.trim() : (!!target && !!draft.trim());
    const submit = () => {
      if (!canSubmit) return;
      if (mode === 'rename') runRename(false);
      else create.mutate(draft.trim(), {
        onSuccess: () => { announce(t('Created {name}', { name: draft.trim() })); reset(); },
        onError: (error) => setErr(failure(error, t('Could not create'))),
      });
    };
    return (
      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <h2 className={styles.panelTitle}>{title}</h2>
          <button type="button" className={styles.iconBtn} onClick={reset} aria-label={t('Cancel')}>
            <X size={16} aria-hidden="true" focusable={false} />
          </button>
        </div>
        {mode === 'rename' && (nodeByPath.get(target)?.children.length ?? 0) > 0 && (
          <p className={styles.note}>
            {t('This value has sub-values, so they are renamed too.')}
          </p>
        )}
        <label className={styles.field}>
          <span className={styles.label}>{label}</span>
          <input
            className={styles.input}
            value={draft}
            autoFocus
            placeholder={placeholder}
            aria-invalid={!!err}
            aria-describedby={err ? 'cc-value-editor-err' : undefined}
            onChange={(e) => { setDraft(e.target.value); setConflicts(null); }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } if (e.key === 'Escape') reset(); }}
          />
        </label>
        {/* Pre-check: the merge list we can see from the cached tree, before the
            server has been asked. The server still decides. */}
        {predictedMerges.length > 0 && (
          <p className={styles.warn} role="status">
            {t('{count} existing value(s) will be merged into this one. You will be asked to confirm.', { count: predictedMerges.length })}
          </p>
        )}
        {conflicts?.length ? (
          <div className={styles.confirm} role="alert">
            <p className={styles.confirmText}>
              {t('This will merge {count} value(s) into existing ones. There is no undo.', { count: conflicts.length })}
            </p>
            <ul className={styles.confirmList}>
              {conflicts.map((c) => (
                <li key={c.from}>{c.from} → {c.into} ({t('{count} books', { count: c.books })})</li>
              ))}
            </ul>
            <button type="button" className={styles.primary} disabled={busy}
              onClick={() => runRename(true)}>{t('Merge and rename')}</button>
            <button type="button" className={styles.ghost} onClick={() => setConflicts(null)}>{t('Cancel')}</button>
          </div>
        ) : (
          <>
            {err && <span id="cc-value-editor-err" className={styles.err} role="alert">{err}</span>}
            <div className={styles.actions}>
              <button type="button" className={styles.primary} disabled={busy || !canSubmit} onClick={submit}>
                <Check size={15} aria-hidden="true" focusable={false} /> {t('Save')}
              </button>
              <button type="button" className={styles.ghost} onClick={reset}>{t('Cancel')}</button>
            </div>
          </>
        )}
      </div>
    );
  }

  // "Merge Into" is pressed on the row that should SURVIVE. The destination is
  // therefore that row, not a typed name: the user picked it by clicking, which
  // is what removes the old failure of having to remember a full dotted path.
  if (mode === 'mergeInto') {
    const run = () => {
      merge.mutate({ paths: mergeSources, into: target }, {
        onSuccess: (r) => {
          // r.books is how many books were re-pointed at the survivor -- the
          // number that tells the user whether anything actually moved.
          announce(t('Merged {count} values into {name}, moving {books} book(s)',
            { count: mergeSources.length, name: target, books: r.books ?? 0 }));
          reset();
        },
        onError: (error) => setErr(failure(error, t('Could not merge'))),
      });
    };
    return (
      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <h2 className={styles.panelTitle}>{t('Merge into {name}', { name: target })}</h2>
          <button type="button" className={styles.iconBtn} onClick={reset} aria-label={t('Cancel')}>
            <X size={16} aria-hidden="true" focusable={false} />
          </button>
        </div>
        <div className={styles.confirm} role="alert">
          <p className={styles.confirmText}>
            {t('These values are folded into {name}. Their books keep a value, but the values themselves are gone. There is no undo.', { name: target })}
          </p>
          <ul className={styles.confirmList}>
            {mergeSources.map((p) => <li key={p}>{p}</li>)}
          </ul>
          {err && <span className={styles.err} role="alert">{err}</span>}
          <div className={styles.actions}>
            <button type="button" className={styles.primary} disabled={busy || mergeSources.length === 0}
              onClick={run}>
              <Merge size={15} aria-hidden="true" focusable={false} /> {t('Merge')}
            </button>
            <button type="button" className={styles.ghost} onClick={reset}>{t('Cancel')}</button>
          </div>
        </div>
      </div>
    );
  }

  if (mode === 'delete') {
    return (
      <div className={styles.panel}>
        <div className={styles.panelHead}>
          <h2 className={styles.panelTitle}>{t('Delete values')}</h2>
          <button type="button" className={styles.iconBtn} onClick={reset} aria-label={t('Cancel')}>
            <X size={16} aria-hidden="true" focusable={false} />
          </button>
        </div>
        <div className={styles.confirm} role="alert">
          <p className={styles.confirmText}>
            {t('The selected values are removed from {count} book(s). The books are kept — they simply lose the value. There is no undo.', { count: deleteTargets.length })}
          </p>
          <ul className={styles.confirmList}>
            {deleteTargets.map((p) => <li key={p}>{p}</li>)}
          </ul>
          {err && <span className={styles.err} role="alert">{err}</span>}
          <div className={styles.actions}>
            <button type="button" className={styles.danger} disabled={busy || deleteTargets.length === 0}
              onClick={() => remove.mutate(deleteTargets, {
                onSuccess: (r) => { announce(t('Deleted {count} values', { count: r.deleted?.length ?? 0 })); reset(); },
                onError: (error) => setErr(failure(error, t('Could not delete'))),
              })}>
              <Trash2 size={15} aria-hidden="true" focusable={false} /> {t('Delete')}
            </button>
            <button type="button" className={styles.ghost} onClick={reset}>{t('Cancel')}</button>
          </div>
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------------ idle
  return (
    <div className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle}>{t('Edit values')}</h2>
        <button type="button" className={styles.iconBtn} onClick={onDone} aria-label={t('Done')}>
          <X size={16} aria-hidden="true" focusable={false} />
        </button>
      </div>
      {/* The buffer has to be VISIBLE. A cut that lives only in component state
          is lost the moment the user scrolls away, and there is no undo. */}
      {cutPath && (
        <div className={styles.banner} role="status">
          <Scissors size={15} aria-hidden="true" focusable={false} />
          <span className={styles.bannerText}>
            {t('{name} is ready to paste. Choose where it should go.', { name: cutPath })}
          </span>
          {/* The Paste button on a row is gated on that row being ticked, so
              without this the banner reads as an instruction with no way to
              follow it: every row is greyed out and nothing says why. */}
          <span className={styles.bannerHint}>
            {t('Tick the value it should go under, then press Paste on that row. The valid rows are highlighted.')}
          </span>
          <button type="button" className={styles.bannerBtn} disabled={busy}
            onClick={() => runPaste('')}>
            {t('Paste at top level')}
          </button>
          <button type="button" className={styles.bannerBtn} onClick={() => setCutPath(null)}>
            {t('Cancel')}
          </button>
        </div>
      )}
      {/* Every failure in this panel — a refused move, a merge conflict, a lost
          permission — lands here, beside the banner, where it cannot be missed. */}
      {err && (
        <div className={styles.banner} role="alert">
          <X size={15} aria-hidden="true" focusable={false} />
          <span className={styles.bannerText}>{err}</span>
        </div>
      )}
      <p className={styles.note}>
        {hierarchical
          ? t('Every action is on the row it applies to. Renaming renames everything below the value. To move a value, tick it and press Cut, then tick the value it should go under and press Paste.')
          : t('Every action is on the row it applies to. To merge, tick two or more values, then use Merge on the one that should survive.')}
      </p>
      <ul className={styles.tree}>
        {rows.map(({ node, depth }) => {
          // The server's verdict on this node, not a local `children.length`
          // check. The fallback keeps an older server's nodes working.
          const isLeaf = node.available
            ? node.available.can_delete
            : node.children.length === 0;
          const isCut = cutPath === node.path;
          const isTicked = !!checked[node.path];
          const pasteState = pasteAvailability(cutPath, node.path, isTicked);
          // Marked from the cycle rule alone, so the rows that CAN receive
          // the held value are visible before any of them is ticked.
          const isPasteTarget = hierarchical && isValidPasteTarget(cutPath, node.path);
          const title = hierarchical ? `${depth * INDENT_PX + INDENT_ROOT_PX}px` : undefined;
          return (
            <li key={node.path}
              className={`${styles.row} ${isCut ? styles.rowCut : ''} ${isPasteTarget ? styles.rowPasteTarget : ''}`}>
              <label className={styles.check} style={title ? { paddingLeft: title } : undefined}>
                <input type="checkbox" checked={!!checked[node.path]} onChange={() => toggle(node.path)} />
                <span className={styles.rowName}>{node.name}</span>
                <span className={styles.rowCount}>{node.total_count}</span>
                {!isLeaf && <span className={styles.rowTag}>{t('has sub-values')}</span>}
              </label>
              {/* Actions live ON the row so there is never any doubt which value
                  they apply to — the failure of the old bottom action bar was
                  having to remember. */}
              <span className={styles.rowActions}>
                <button type="button" className={styles.rowBtn}
                  aria-label={t('Rename {name}', { name: node.name })}
                  title={t('Rename')}
                  onClick={() => { setTarget(node.path); setDraft(node.name); setConflicts(null); setMode('rename'); }}>
                  <Pencil size={14} aria-hidden="true" focusable={false} />
                </button>
                <button type="button" className={styles.rowBtn}
                  disabled={busy || !canCut(isTicked)}
                  aria-label={t('Cut {name}', { name: node.name })}
                  title={isTicked ? t('Cut') : t('Tick this value before cutting it')}
                  onClick={() => {
                    setCutPath(node.path);
                    setErr('');
                    announce(t('{name} is ready to paste. Choose where it should go.', { name: node.path }));
                  }}>
                  <Scissors size={14} aria-hidden="true" focusable={false} />
                </button>
                {hierarchical && (
                  <button type="button" className={styles.rowBtn}
                    disabled={!pasteState.ok || busy}
                    aria-label={t('Paste under {name}', { name: node.name })}
                    title={pasteReason(pasteState.reason)}
                    onClick={() => runPaste(node.path)}>
                    <ClipboardPaste size={14} aria-hidden="true" focusable={false} />
                  </button>
                )}
                {/* Merge: available on any ticked leaf that has another value
                    ticked too. Clicking makes THIS row the survivor and folds
                    every OTHER ticked value into it, so the destination is
                    chosen by clicking instead of typed. */}
                <button type="button" className={styles.rowBtn}
                  disabled={busy || !mergeAvailability(chosen, node.path, canBeSource).ok}
                  aria-label={t('Merge the selected values into {name}', { name: node.name })}
                  title={mergeTitle(node.path)}
                  onClick={() => { setTarget(node.path); setErr(''); setMode('mergeInto'); }}>
                  <Merge size={14} aria-hidden="true" focusable={false} />
                </button>
                {/* Delete is leaves-only, matching the server. A row with children
                    is disabled rather than hidden, so the rule stays visible. */}
                <button type="button" className={styles.rowBtn}
                  disabled={busy}
                  aria-label={t('Delete {name}', { name: node.name })}
                  title={isLeaf
                    ? t('Delete')
                    : t('Only values without sub-values can be deleted. Move or rename it first.')}
                  onClick={() => { setDeleteTargets([node.path]); setErr(''); setMode('delete'); }}>
                  <Trash2 size={14} aria-hidden="true" focusable={false} />
                </button>
              </span>
            </li>
          );
        })}
      </ul>
      {/* Merge and Delete are no longer here: both act on a specific row, so
          they live on the row next to Rename and Cut. A bottom button could only
          ever be ambiguous about WHICH value it meant.

          The error is NOT rendered here. It used to be, below the tree — and a
          real library's value list is long enough that "Something is already
          there" / "Value not found" landed off-screen, so a failed move or
          paste read as "nothing happened". It now sits beside the paste
          banner, inside the first screenful (see the role="alert" above). */}
      <div className={styles.actions}>
        <button type="button" className={styles.action} onClick={() => { setMode('create'); setDraft(''); }}>
          <Plus size={15} aria-hidden="true" focusable={false} /> {t('Create new')}
        </button>
      </div>
    </div>
  );
}