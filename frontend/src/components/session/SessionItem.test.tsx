import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { SessionItem } from './SessionItem';
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
