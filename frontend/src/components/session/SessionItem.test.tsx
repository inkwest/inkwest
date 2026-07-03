import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SessionItem } from './SessionItem';
import { useSessionsStore } from '@/store/sessions';
import type { Session } from '@/types/api';

const session: Session = {
  id: 's1',
  title: 'My chat',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
};

const dragHandleProps = { draggable: true, onDragStart: () => {}, onDragEnd: () => {} };

function renderItem(isDragging: boolean) {
  return render(
    <MemoryRouter>
      <SessionItem session={session} isDragging={isDragging} dragHandleProps={dragHandleProps} />
    </MemoryRouter>,
  );
}

describe('SessionItem drag source', () => {
  // Regression guard: Firefox fires no dragend if the drag source is removed
  // mid-drag, which left the row stuck dimmed. The grip must stay mounted while
  // dragging even though the pointer has left the row (so isHovered is false).
  it('keeps the grip mounted while dragging without hover', () => {
    renderItem(true);
    expect(screen.getByLabelText('Drag to reorder')).toBeInTheDocument();
  });

  it('hides the grip when neither hovered nor dragging', () => {
    renderItem(false);
    expect(screen.queryByLabelText('Drag to reorder')).not.toBeInTheDocument();
  });
});

describe('SessionItem shared indicator', () => {
  function renderWith(overrides: Partial<Session>) {
    return render(
      <MemoryRouter>
        <SessionItem
          session={{ ...session, ...overrides }}
          isDragging={false}
          dragHandleProps={dragHandleProps}
        />
      </MemoryRouter>,
    );
  }

  it('flags an owned conversation that is shared with others', () => {
    renderWith({ is_owner: true, share_count: 3 });
    expect(screen.getByTitle('Shared with 3')).toBeInTheDocument();
  });

  it('shows no indicator when an owned conversation is not shared', () => {
    renderWith({ is_owner: true, share_count: 0 });
    expect(screen.queryByTitle(/Shared with/)).not.toBeInTheDocument();
    expect(screen.queryByText('External')).not.toBeInTheDocument();
  });

  it('marks a conversation shared *with* the user as External, not as shared-out', () => {
    renderWith({ is_owner: false, owner_display_name: 'Alice', share_count: 0 });
    expect(screen.getByText('External')).toBeInTheDocument();
    expect(screen.queryByTitle(/Shared with/)).not.toBeInTheDocument();
  });
});

describe('SessionItem timestamp tracks the active sort', () => {
  // Distinct ages so the shown time reveals which field is displayed.
  const timed: Session = {
    id: 's1',
    title: 'T',
    created_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(), // 3h ago
    updated_at: new Date().toISOString(),
    last_message_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(), // 5m ago
  };

  function renderWithSort(sortMode: 'activity' | 'created') {
    useSessionsStore.setState({ sortMode });
    return render(
      <MemoryRouter>
        <SessionItem session={timed} isDragging={false} dragHandleProps={dragHandleProps} />
      </MemoryRouter>,
    );
  }

  it('shows last-activity time under the activity sort', () => {
    renderWithSort('activity');
    expect(screen.getByText('5m ago')).toBeInTheDocument();
  });

  it('shows creation time under the created sort (matches the ordering)', () => {
    renderWithSort('created');
    expect(screen.getByText('3h ago')).toBeInTheDocument();
  });
});
