import { useEffect, useState } from 'react';
import { Link, useSearch } from 'wouter';
import { ChevronLeft, ChevronRight, ChevronRight as Chevron } from 'lucide-react';
import { useColumns, useCcTree, useCcBooks, useMe } from '../lib/queries';
import type { CcNode } from '../lib/api';
import { BookCard } from '../components/BookCard';
import { SpinnerCentered } from '../components/Spinner';
import { EmptyState } from '../components/EmptyState';
import { useT } from '../lib/i18n';
import { canReadBooks } from '../lib/permissions';
import styles from './CcBrowse.module.css';

/** True when `selected` is `nodePath` itself or a deeper descendant, so the
 *  ancestors of the active node render expanded (mirrors the classic tree's
 *  `current_path.startswith(node.path)` server-side open state). */
function isAncestorPath(selected: string, nodePath: string): boolean {
  return !!selected && (selected === nodePath || selected.startsWith(nodePath + '.'));
}

/** One collapsible tree node. Mirrors the classic UI's <details>-based tree
 *  (cps/templates/macros/hierarchy.html::render_details_tree): the browser's
 *  native open/close state is the expansion state, so no per-node React state
 *  is needed and user-toggled branches survive re-renders for free. */
function TreeNode({ node, colId, selected }: { node: CcNode; colId: string; selected: string }) {
  const t = useT();
  const href = `/cc/${colId}?path=${encodeURIComponent(node.path)}`;
  const active = selected === node.path;
  return (
    <li className={styles.treeNode}>
      <details open={isAncestorPath(selected, node.path)}>
        <summary>
          <Chevron size={14} className={styles.chevron} aria-hidden="true" focusable={false} />
          <Link href={href}
            className={active ? `${styles.nodeLink} ${styles.nodeLinkActive}` : styles.nodeLink}
            aria-current={active ? 'page' : undefined}>
            {node.name}
          </Link>
          <span className={styles.badge} aria-label={t('{count} books', { count: node.total_count })}>
            {node.total_count}
          </span>
        </summary>
        {node.children.length > 0 && (
          <ul className={styles.children}>
            {node.children.map((child) => (
              <TreeNode key={child.path} node={child} colId={colId} selected={selected} />
            ))}
          </ul>
        )}
      </details>
    </li>
  );
}

/** The tree for one column, or a loading/empty state. */
function ColumnTree({ colId, selected }: { colId: string; selected: string }) {
  const t = useT();
  const { data, isLoading } = useCcTree(colId);
  if (isLoading) return <SpinnerCentered size={40} />;
  if (!data || data.nodes.length === 0) {
    return <EmptyState title={t('No values in this column yet')}
      message={t('Books tagged with values in this column will appear here.')} />;
  }
  return (
    <ul className={styles.tree} role="tree">
      {data.nodes.map((node) => (
        <TreeNode key={node.path} node={node} colId={colId} selected={selected} />
      ))}
    </ul>
  );
}

/** Books under the selected node, paged. An empty `path` lists every book
 *  carrying any value in the column. */
function NodeBooks({ colId, path }: { colId: string; path: string }) {
  const t = useT();
  const me = useMe().data;
  const [page, setPage] = useState(1);
  const { data, isLoading } = useCcBooks(colId, path, page);

  // Reset paging when the node changes.
  useEffect(() => { setPage(1); }, [colId, path]);

  const total = data?.total ?? 0;
  const perPage = data?.per_page ?? 24;
  const lastPage = Math.max(1, Math.ceil(total / perPage));

  return (
    <section className={styles.booksSection} aria-label={t('Books')}>
      <div className={styles.booksHeader}>
        <h2 className={styles.booksTitle}>{path || t('All')}</h2>
        <span className={styles.count}>{t('{count} books', { count: total })}</span>
      </div>
      {isLoading ? <SpinnerCentered size={40} /> : (data?.items.length ?? 0) === 0 ? (
        <EmptyState title={t('No books here yet')}
          message={t('Try a different category or page.')} />
      ) : (
        <>
          <div className={styles.bookGrid}>
            {data!.items.map((book) => (
              <BookCard key={book.id} book={book} canRead={canReadBooks(me)} />
            ))}
          </div>
          {lastPage > 1 && (
            <nav className={styles.pager} aria-label={t('Pagination')}>
              <button type="button" className={styles.pageButton} disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}>
                <ChevronLeft size={16} aria-hidden="true" focusable={false} />
                {t('Previous')}
              </button>
              <span className={styles.pageInfo}>
                {t('Page {page} of {last}', { page, last: lastPage })}
              </span>
              <button type="button" className={styles.pageButton} disabled={page >= lastPage}
                onClick={() => setPage((p) => Math.min(lastPage, p + 1))}>
                {t('Next')}
                <ChevronRight size={16} aria-hidden="true" focusable={false} />
              </button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

/** /cc — the list of browsable custom columns; /cc/:id — one column's tree
 *  plus the books under the selected node (?path=). The SPA counterpart of
 *  the classic UI's /custom_column/<id>[/<path>] views. */
export function CcBrowse({ id }: { id?: string }) {
  const t = useT();
  const search = useSearch();
  const path = new URLSearchParams(search).get('path') ?? '';
  // Hook order: both hooks run on both modes; only the relevant one is enabled.
  const columns = useColumns(!id);
  const tree = useCcTree(id ?? '', !!id);

  // Column list mode: every browsable column as a card.
  if (!id) {
    return (
      <main className={styles.container}>
        <div className={styles.header}>
          <h1 className={styles.title}>{t('Custom Columns')}</h1>
          {!!columns.data?.items.length && (
            <span className={styles.count}>
              {t('{count} columns', { count: columns.data.items.length })}
            </span>
          )}
        </div>
        {columns.isLoading ? <SpinnerCentered size={40} /> : (columns.data?.items.length ?? 0) === 0 ? (
          <EmptyState title={t('No custom columns to browse')}
            message={t('Tag-like custom columns defined in the library appear here.')} />
        ) : (
          <ul className={styles.grid}>
            {columns.data!.items.map((col) => (
              <li key={col.id}>
                <Link href={`/cc/${col.id}`} className={styles.columnCard}>
                  <span className={styles.columnName}>{col.name}</span>
                  {col.hierarchical && (
                    <span className={styles.badge}>{t('Tree')}</span>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </main>
    );
  }

  // Column mode: tree + books under the selected node.
  return (
    <main className={styles.container}>
      <Link href="/cc" className={styles.back}>
        <ChevronLeft size={16} aria-hidden="true" focusable={false} />
        {t('Custom Columns')}
      </Link>
      <div className={styles.header}>
        <h1 className={styles.title}>{t('Browse by Column')}</h1>
      </div>
      <p className={styles.hint}>
        {t('Select a category to see the books assigned to it and all of its sub-categories.')}
      </p>
      {tree.isLoading ? <SpinnerCentered size={40} /> : <ColumnTree colId={id} selected={path} />}
      <NodeBooks colId={id} path={path} />
    </main>
  );
}