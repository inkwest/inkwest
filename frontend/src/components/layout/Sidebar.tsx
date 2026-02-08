import { SessionList } from '@/components/session/SessionList';
import { NewSessionButton } from '@/components/session/NewSessionButton';

export function Sidebar() {
  return (
    <div className="w-full bg-gray-800 border-r border-gray-700 flex flex-col h-full">
      <div className="h-14 px-4 border-b border-gray-700 flex items-center">
        <h1 className="text-xl font-bold">Inkwest</h1>
      </div>
      <div className="p-2">
        <NewSessionButton />
      </div>
      <div className="flex-1 overflow-y-auto">
        <SessionList />
      </div>
    </div>
  );
}
