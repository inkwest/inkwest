import { useState } from 'react';
import { Plus } from 'lucide-react';
import { NewSessionDialog } from './NewSessionDialog';

export function NewSessionButton() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium transition-colors"
      >
        <Plus className="h-4 w-4" />
        New Session
      </button>
      <NewSessionDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}
