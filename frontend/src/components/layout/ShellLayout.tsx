import { useCallback, useEffect, useRef, type ReactNode } from 'react';
import { Header } from './Header';
import { UpdateBanner } from './UpdateBanner';
import { AnnouncementBanners } from './AnnouncementBanners';
import { BackendBanner } from './BackendBanner';
import { PushOptInBanner } from './PushOptInBanner';
import { useSettingsStore } from '@/store/settings';

const DEFAULT_WIDTH = 256;
const MIN_WIDTH = 200;
const MAX_WIDTH = 480;

interface ShellLayoutProps {
  sidebar: ReactNode;
  mainClassName?: string;
  children: ReactNode;
}

export function ShellLayout({ sidebar, mainClassName = 'flex-1 overflow-hidden min-h-0', children }: ShellLayoutProps) {
  const { sidebarOpen, setSidebarOpen, sidebarWidth, setSidebarWidth } = useSettingsStore();
  const dragging = useRef(false);

  const onMouseMove = useCallback(
    (e: MouseEvent) => {
      if (!dragging.current) return;
      setSidebarWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, e.clientX)));
    },
    [setSidebarWidth],
  );

  const onMouseUp = useCallback(() => {
    dragging.current = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  }, []);

  useEffect(() => {
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, [onMouseMove, onMouseUp]);

  const onHandleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  const onHandleDoubleClick = useCallback(() => {
    setSidebarWidth(DEFAULT_WIDTH);
  }, [setSidebarWidth]);

  return (
    <div className="flex flex-col h-screen bg-gray-900 text-white overflow-hidden">
      {/* Full-width notices pinned to the very top of the window. */}
      <BackendBanner />
      <UpdateBanner />
      <AnnouncementBanners />
      <PushOptInBanner />
      <div className="flex flex-1 min-h-0">
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-30 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 w-64 transition-transform duration-200 md:relative md:translate-x-0 md:w-auto ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
        style={{ width: undefined }}
      >
        <div className="h-full md:hidden w-64">
          {sidebar}
        </div>
        <div className="h-full hidden md:block" style={{ width: sidebarWidth }}>
          {sidebar}
        </div>
      </aside>

      <div
        className="hidden md:block w-1 cursor-col-resize bg-gray-700 hover:bg-blue-500 active:bg-blue-500 transition-colors flex-shrink-0"
        onMouseDown={onHandleMouseDown}
        onDoubleClick={onHandleDoubleClick}
      />

      <div className="flex-1 flex flex-col min-w-0">
        <Header />
        <main className={mainClassName}>
          {children}
        </main>
      </div>
      </div>
    </div>
  );
}
