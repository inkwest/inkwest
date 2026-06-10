import { Plus } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

// Routes to the session root, where the conversation is composed and the session
// is created silently on the first interaction (message or external-access grant).
export function NewSessionButton() {
  const navigate = useNavigate();

  return (
    <button
      onClick={() => navigate('/')}
      className="w-full flex items-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium transition-colors"
    >
      <Plus className="h-4 w-4" />
      New Session
    </button>
  );
}
