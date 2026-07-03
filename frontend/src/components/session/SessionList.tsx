import { Fragment, useEffect, useState, useRef, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { Search, X, Loader2, RefreshCw, ArrowDown, ArrowUp, ListFilter, RotateCcw } from 'lucide-react';
import { useSessionsStore, type DateRange } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { SessionItem } from './SessionItem';
import { SearchSessionHit } from '@/types/api';
import type { SessionSort, ShareFilter } from '@/services/api';
import { formatRelativeTime, cn } from '@/lib/utils';

const DEBOUNCE_MS = 300;
// Mirror the backend's MAX_SEARCH_TOKENS cap: it only matches the first N tokens, so
// highlighting beyond N would mark words the result wasn't actually selected for.
const MAX_SEARCH_TOKENS = 10;

const SORT_OPTIONS: { value: SessionSort; label: string }[] = [
  { value: 'manual', label: 'Manual order' },
  { value: 'activity', label: 'Last activity' },
  { value: 'created', label: 'Created' },
];

// Sharing-bucket filters (checkboxes; OR'd server-side). Mirrors the backend's
// allowed `share` values in list_sessions.
const SHARE_FILTERS: { value: ShareFilter; label: string }[] = [
  { value: 'shared_with_me', label: 'Shared with me' },
  { value: 'shared_by_me', label: 'Shared by me' },
  { value: 'private', label: 'Private' },
];

// Relative "last activity" date presets.
const DATE_RANGES: { value: DateRange; label: string }[] = [
  { value: 'all', label: 'All time' },
  { value: 'today', label: 'Today' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
  { value: 'custom', label: 'Custom range' },
];

function HighlightText({ text, query }: { text: string; query: string }) {
  // Word-independent search: highlight every query token wherever it occurs, in any
  // order — a single contiguous indexOf would miss non-adjacent multi-word matches.
  const tokens = query.split(/\s+/).filter(Boolean).slice(0, MAX_SEARCH_TOKENS);
  if (tokens.length === 0) return <>{text}</>;
  const escaped = tokens.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  // Capturing group → split keeps matches at odd indices.
  const parts = text.split(new RegExp(`(${escaped.join('|')})`, 'gi'));
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <mark key={i} className="bg-yellow-500/30 text-yellow-200 rounded-sm px-0.5">{part}</mark>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        )
      )}
    </>
  );
}

function SearchResultItem({ hit, query, onClick }: { hit: SearchSessionHit; query: string; onClick?: () => void }) {
  return (
    <div className="space-y-1">
      <Link
        to={`/session/${hit.id}`}
        className="block px-3 py-2 rounded-lg text-sm text-gray-300 hover:bg-gray-700/50 transition-colors"
        onClick={onClick}
      >
        <div className="font-medium truncate">
          <HighlightText text={hit.title || 'New conversation'} query={query} />
        </div>
        <div className="flex items-center gap-1.5 text-xs text-gray-500">
          {hit.cluster && (
            <>
              <span className="truncate max-w-[100px]">{hit.cluster.display_name}</span>
              <span className="text-gray-600">&middot;</span>
            </>
          )}
          <span className="shrink-0">{formatRelativeTime(hit.updated_at)}</span>
        </div>
      </Link>
      {hit.matched_messages.length > 0 && (
        <div className="ml-3 pl-3 border-l border-gray-700 space-y-1">
          {hit.matched_messages.map((msg) => (
            <Link
              key={msg.id}
              to={`/session/${hit.id}?m=${msg.id}`}
              className="block px-2 py-1 rounded text-xs text-gray-400 hover:bg-gray-700/30 transition-colors"
              onClick={onClick}
            >
              <span className="text-gray-500 mr-1">{msg.role}:</span>
              <HighlightText text={msg.snippet} query={query} />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export function SessionList() {
  const {
    sessions, fetchSessions, isLoading, error,
    searchResults, isSearching, searchGlobal, clearSearch,
    moveSession, persistSessionOrder,
    sortMode, sortOrder, filters, dateRange, customFrom, customTo, total,
    setSortMode, setSortOrder, toggleFilter, setFilters, setDateRange, setCustomRange, resetView,
    loadMoreSessions, isLoadingMore,
  } = useSessionsStore();
  const setSidebarOpen = useSettingsStore((s) => s.setSidebarOpen);
  // Display-only filter: a session gets its title from the first user message,
  // so a title-less session ("New conversation") has no interaction yet.
  const visibleSessions = sessions.filter((s) => s.title);
  // Drag reordering is only meaningful in the manual arrangement; other sorts are
  // derived, so a drag can't persist a stable position there.
  const canReorder = sortMode === 'manual';
  // "Showing everything" = no sharing filter (empty) or all buckets selected — both
  // return the full set. A proper subset, or a date window, is real narrowing.
  const allShown = filters.length === 0 || filters.length === SHARE_FILTERS.length;
  const isNarrowed = !allShown || dateRange !== 'all';
  // Dot / reset enable only when the view differs from the default (latest activity,
  // descending, all buckets, all time). The default view shows no dot.
  const controlsActive = sortMode !== 'activity' || sortOrder !== 'desc' || isNarrowed;
  const [searchQuery, setSearchQuery] = useState('');
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [showControls, setShowControls] = useState(false);
  const controlsRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

  // Close the sort/filter popover on an outside click or Escape.
  useEffect(() => {
    if (!showControls) return;
    const onPointerDown = (e: MouseEvent) => {
      if (controlsRef.current && !controlsRef.current.contains(e.target as Node)) {
        setShowControls(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setShowControls(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [showControls]);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  const handleSearchChange = useCallback((value: string) => {
    setSearchQuery(value);
    clearTimeout(debounceRef.current);

    if (!value.trim() || value.trim().length < 2) {
      clearSearch();
      return;
    }

    debounceRef.current = setTimeout(() => {
      searchGlobal(value.trim());
    }, DEBOUNCE_MS);
  }, [searchGlobal, clearSearch]);

  const handleClear = useCallback(() => {
    setSearchQuery('');
    clearSearch();
  }, [clearSearch]);

  // Tracks whether the drag actually moved anything, so a grab-and-release with no
  // reorder doesn't fire a needless persist (and its cross-tab notify).
  const movedRef = useRef(false);

  const handleDragStart = (e: React.DragEvent, id: string) => {
    setDraggingId(id);
    movedRef.current = false;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);
  };

  const handleDragOver = (e: React.DragEvent, overId: string) => {
    if (!draggingId) return;
    // Always accept while dragging — once the live reorder puts the dragged row
    // under the cursor, draggingId === overId is the steady state, and Firefox
    // treats an unaccepted release as an unhandled drop (default handling, no
    // clean drop) instead of completing the move.
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (draggingId === overId) return;
    moveSession(draggingId, overId); // live, optimistic reorder
    movedRef.current = true;
  };

  const handleDragEnd = () => {
    if (draggingId && movedRef.current) persistSessionOrder(); // save only a real reorder
    setDraggingId(null);
  };

  // A remote sync can delete the dragged session mid-drag; its dragend then
  // never fires (removed drag sources don't get one), which would leave the
  // list stuck accepting dragovers for a dead id.
  useEffect(() => {
    if (draggingId && !sessions?.some((s) => s.id === draggingId)) {
      setDraggingId(null);
    }
  }, [sessions, draggingId]);

  useEffect(() => {
    return () => clearTimeout(debounceRef.current);
  }, []);

  const isInSearchMode = searchQuery.trim().length >= 2;

  if (isLoading && !isInSearchMode) {
    return <div className="p-4 text-gray-400">Loading...</div>;
  }

  // A failed initial load (often a transient 500 while the backend is still warming
  // up on a cold start) shouldn't dead-end the sidebar: explain it and offer a retry
  // so the user can recover without a full page reload.
  if (error && !isInSearchMode) {
    return (
      <div className="p-4 text-sm">
        <p className="text-gray-300">Couldn&apos;t load your sessions.</p>
        <p className="mt-1 text-xs text-gray-500">The server may be starting up or briefly unavailable.</p>
        <button
          onClick={() => fetchSessions()}
          className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-gray-700 px-2.5 py-1.5 text-xs text-gray-200 hover:bg-gray-600 transition-colors"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <div className="px-2 pt-2 pb-1 flex items-center gap-1">
        <div className="relative flex-1">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => handleSearchChange(e.target.value)}
            placeholder="Search all sessions..."
            className="w-full bg-gray-700 border border-gray-600 rounded-md pl-8 pr-7 py-1.5 text-xs text-gray-300 placeholder-gray-500 focus:outline-none focus:border-gray-500"
          />
          {searchQuery && (
            <button
              onClick={handleClear}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>

        {!isInSearchMode && (
          <div className="relative" ref={controlsRef}>
            <button
              type="button"
              onClick={() => setShowControls((v) => !v)}
              title="Sort & filter"
              aria-label="Sort and filter"
              aria-expanded={showControls}
              className={cn(
                'relative shrink-0 p-1.5 rounded-md border border-gray-600 hover:bg-gray-600',
                showControls ? 'bg-gray-600 text-gray-200' : 'bg-gray-700 text-gray-300'
              )}
            >
              <ListFilter className="h-3.5 w-3.5" />
              {controlsActive && (
                <span
                  data-testid="sort-active-dot"
                  className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-blue-400"
                />
              )}
            </button>

            {showControls && (
              <div className="absolute right-0 top-full z-30 mt-1 w-52 rounded-md border border-gray-600 bg-gray-800 p-2 shadow-lg space-y-2">
                <div className="space-y-0.5">
                  <div className="px-1 text-[10px] font-medium uppercase tracking-wide text-gray-500">
                    Sort
                  </div>
                  {SORT_OPTIONS.map(({ value, label }) => {
                    const selected = sortMode === value;
                    return (
                      <div key={value} className="flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => setSortMode(value)}
                          className={cn(
                            'flex-1 text-left rounded px-2 py-1 text-xs',
                            selected
                              ? 'bg-gray-700 text-gray-100'
                              : 'text-gray-300 hover:bg-gray-700/60'
                          )}
                        >
                          {label}
                        </button>
                        {selected && value !== 'manual' && (
                          <button
                            type="button"
                            onClick={() => setSortOrder(sortOrder === 'desc' ? 'asc' : 'desc')}
                            title={sortOrder === 'desc' ? 'Newest first' : 'Oldest first'}
                            aria-label={sortOrder === 'desc' ? 'Newest first' : 'Oldest first'}
                            className="shrink-0 p-1 rounded text-gray-300 hover:bg-gray-700"
                          >
                            {sortOrder === 'desc' ? (
                              <ArrowDown className="h-3.5 w-3.5" />
                            ) : (
                              <ArrowUp className="h-3.5 w-3.5" />
                            )}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>

                <div className="space-y-0.5 border-t border-gray-700 pt-2">
                  <div className="px-1 text-[10px] font-medium uppercase tracking-wide text-gray-500">
                    Date
                  </div>
                  {DATE_RANGES.map(({ value, label }) => (
                    <button
                      key={value}
                      type="button"
                      onClick={() => setDateRange(value)}
                      className={cn(
                        'w-full text-left rounded px-2 py-1 text-xs',
                        dateRange === value
                          ? 'bg-gray-700 text-gray-100'
                          : 'text-gray-300 hover:bg-gray-700/60'
                      )}
                    >
                      {label}
                    </button>
                  ))}
                  {dateRange === 'custom' && (
                    <div className="space-y-1 px-2 pt-1">
                      <label className="flex items-center justify-between gap-2 text-xs text-gray-400">
                        <span>From</span>
                        <input
                          type="date"
                          value={customFrom}
                          max={customTo || undefined}
                          onChange={(e) => setCustomRange(e.target.value, customTo)}
                          className="bg-gray-700 border border-gray-600 rounded px-1.5 py-0.5 text-xs text-gray-200 focus:outline-none focus:border-gray-500"
                        />
                      </label>
                      <label className="flex items-center justify-between gap-2 text-xs text-gray-400">
                        <span>To</span>
                        <input
                          type="date"
                          value={customTo}
                          min={customFrom || undefined}
                          onChange={(e) => setCustomRange(customFrom, e.target.value)}
                          className="bg-gray-700 border border-gray-600 rounded px-1.5 py-0.5 text-xs text-gray-200 focus:outline-none focus:border-gray-500"
                        />
                      </label>
                    </div>
                  )}
                </div>

                <div className="space-y-0.5 border-t border-gray-700 pt-2">
                  <div className="px-1 text-[10px] font-medium uppercase tracking-wide text-gray-500">
                    Show
                  </div>
                  <label className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-300 cursor-pointer select-none hover:bg-gray-700/60">
                    <input
                      type="checkbox"
                      checked={filters.length === SHARE_FILTERS.length}
                      onChange={() =>
                        setFilters(
                          filters.length === SHARE_FILTERS.length
                            ? []
                            : SHARE_FILTERS.map((f) => f.value)
                        )
                      }
                      className="h-3 w-3 rounded border-gray-600 bg-gray-700 text-blue-500 focus:ring-0"
                    />
                    Show all
                  </label>
                  {SHARE_FILTERS.map(({ value, label }) => (
                    <label
                      key={value}
                      className="flex items-center gap-2 rounded px-2 py-1 text-xs text-gray-300 cursor-pointer select-none hover:bg-gray-700/60"
                    >
                      <input
                        type="checkbox"
                        checked={filters.includes(value)}
                        onChange={() => toggleFilter(value)}
                        className="h-3 w-3 rounded border-gray-600 bg-gray-700 text-blue-500 focus:ring-0"
                      />
                      {label}
                    </label>
                  ))}
                </div>

                <div className="border-t border-gray-700 pt-2">
                  <button
                    type="button"
                    onClick={() => resetView()}
                    disabled={!controlsActive}
                    className="w-full flex items-center justify-center gap-1.5 rounded px-2 py-1 text-xs text-gray-400 hover:bg-gray-700/60 hover:text-gray-200 disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-gray-400"
                  >
                    <RotateCcw className="h-3 w-3" />
                    Reset to default
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="space-y-1 p-2">
        {isInSearchMode ? (
          isSearching ? (
            <div className="flex items-center gap-2 text-gray-400 text-xs px-3 py-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Searching...
            </div>
          ) : searchResults && searchResults.results.length > 0 ? (
            searchResults.results.map((hit) => (
              <SearchResultItem key={hit.id} hit={hit} query={searchQuery} onClick={() => setSidebarOpen(false)} />
            ))
          ) : (
            <div className="text-gray-500 text-xs px-3 py-2">No results</div>
          )
        ) : (
          <>
            {visibleSessions.length === 0 ? (
              <div className="text-gray-500 text-sm px-2">
                {isNarrowed ? 'No conversations match' : 'No sessions yet'}
              </div>
            ) : (
              visibleSessions.map((session) => (
                <div
                  key={session.id}
                  onDragOver={canReorder ? (e) => handleDragOver(e, session.id) : undefined}
                  onDrop={canReorder ? (e) => e.preventDefault() : undefined}
                  className={cn(canReorder && draggingId === session.id && 'opacity-40')}
                >
                  <SessionItem
                    session={session}
                    isDragging={canReorder && draggingId === session.id}
                    dragHandleProps={
                      canReorder
                        ? {
                            draggable: true,
                            onDragStart: (e) => handleDragStart(e, session.id),
                            onDragEnd: handleDragEnd,
                          }
                        : undefined
                    }
                  />
                </div>
              ))
            )}
            {sessions.length < total && (
              <button
                type="button"
                onClick={() => loadMoreSessions()}
                disabled={isLoadingMore}
                className="w-full mt-1 flex items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-gray-400 hover:bg-gray-700/50 disabled:opacity-50"
              >
                {isLoadingMore && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {isLoadingMore ? 'Loading…' : 'Load more'}
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
