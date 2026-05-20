import { ReactNode } from 'react';
import { Header } from './Header';
import type { Status } from '../lib/api';

interface Props {
  connected: boolean;
  status: Status | null;
  children: ReactNode;
}

export function Layout({ connected, status, children }: Props) {
  return (
    <div className="min-h-screen flex flex-col">
      <Header connected={connected} status={status} />
      <main className="flex-1 overflow-y-auto">{children}</main>
    </div>
  );
}
