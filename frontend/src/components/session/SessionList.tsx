import { useEffect, useState, useRef, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { Search, X, Loader2 } from 'lucide-react';
import { useSessionsStore } from '@/store/sessions';
import { useSettingsStore } from '@/store/settings';
import { SessionItem } from './SessionItem';
import { SearchSessionHit } from '@/types/api';
import { formatRelativeTime, cn } from '@/lib/utils';

const DEBOUNCE_MS = 300;

function HighlightText({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-yellow-500/30 text-yellow-200 rounded-sm px-0.5">{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
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
              to={`/session/${hit.id}`}
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
  } = useSessionsStore();
  const setSidebarOpen = useSettingsStore((s) => s.setSidebarOpen);
  const [searchQuery, setSearchQuery] = useState('');
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout>>();

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

  const handleDragStart = (e: React.DragEvent, id: string) => {
    setDraggingId(id);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', id);
  };

  const handleDragOver = (e: React.DragEvent, overId: string) => {
    if (!draggingId || draggingId === overId) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    moveSession(draggingId, overId); // live, optimistic reorder
  };

  const handleDragEnd = () => {
    if (draggingId) persistSessionOrder(); // save the final arrangement once
    setDraggingId(null);
  };

  useEffect(() => {
    return () => clearTimeout(debounceRef.current);
  }, []);

  const isInSearchMode = searchQuery.trim().length >= 2;

  if (isLoading && !isInSearchMode) {
    return <div className="p-4 text-gray-400">Loading...</div>;
  }

  if (error && !isInSearchMode) {
    return <div className="p-4 text-red-400 text-sm">Failed to load sessions</div>;
  }

  return (
    <div className="flex flex-col">
      <div className="px-2 pt-2 pb-1">
        <div className="relative">
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
            {(!sessions || sessions.length === 0) ? (
              <div className="text-gray-500 text-sm px-2">No sessions yet</div>
            ) : (
              sessions.map((session) => (
                <div
                  key={session.id}
                  onDragOver={(e) => handleDragOver(e, session.id)}
                  onDrop={(e) => e.preventDefault()}
                  className={cn(draggingId === session.id && 'opacity-40')}
                >
                  <SessionItem
                    session={session}
                    dragHandleProps={{
                      draggable: true,
                      onDragStart: (e) => handleDragStart(e, session.id),
                      onDragEnd: handleDragEnd,
                    }}
                  />
                </div>
              ))
            )}
          </>
        )}
      </div>
    </div>
  );
}
