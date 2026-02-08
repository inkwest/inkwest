import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { ShellLayout } from './ShellLayout';
import { useSyncSocket } from '@/hooks/useSyncSocket';

export function Layout() {
  useSyncSocket();

  return (
    <ShellLayout sidebar={<Sidebar />}>
      <Outlet />
    </ShellLayout>
  );
}
