import { SessionList } from '@/components/session/SessionList';
import { NewSessionButton } from '@/components/session/NewSessionButton';
import { useUpdateStore } from '@/store/update';
import { formatBuildTime } from '@/lib/utils';

export function Sidebar() {
  // The build the SPA loaded with (captured from /version on connect — ).
  const version = useUpdateStore((s) => s.loadedVersion);
  const buildTime = formatBuildTime(useUpdateStore((s) => s.loadedBuildTimestamp));
  const versionTitle = buildTime ? `Version ${version}\nBuilt at: ${buildTime}` : `Version ${version}`;

  return (
    <div className="w-full bg-gray-800 border-r border-gray-700 flex flex-col h-full">
      <div className="h-14 px-4 border-b border-gray-700 flex flex-col justify-center">
        <div className="flex items-baseline gap-2 min-w-0">
          <h1 className="text-xl font-bold leading-tight">Inkwest</h1>
          {version && (
            <span
              className="font-mono text-[10px] text-gray-500 truncate"
              title={versionTitle}
            >
              {version}
            </span>
          )}
        </div>
        <p className="text-xs text-gray-400 truncate">Secure infrastructure &amp; code assistant</p>
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
