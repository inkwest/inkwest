import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { EgressLog } from './EgressPanel';
import type { EgressEvent } from '@/hooks/useEgressSocket';

function ev(partial: Partial<EgressEvent>): EgressEvent {
  return {
    _id: 0,
    ts: 0,
    decision: 'TCP_TUNNEL',
    status: '200',
    method: 'CONNECT',
    host: 'example.com',
    port: '443',
    bytes: 0,
    client: 'c',
    ...partial,
  };
}

describe('EgressLog row classification', () => {
  it('labels a DENIED line as a proxy block and an upstream 5xx as an upstream error', () => {
    const { queryByText } = render(
      <EgressLog
        connected
        events={[
          ev({ _id: 1, decision: 'TCP_TUNNEL', status: '200', host: 'github.com' }),
          ev({ _id: 2, decision: 'TCP_DENIED', status: '403', host: 'evil.example.com' }),
          ev({ _id: 3, decision: 'TCP_TUNNEL', status: '503', host: 'unreachable.eks.amazonaws.com' }),
        ]}
      />,
    );

    // Only the blocked/errored rows carry a label; the allowed row carries none.
    expect(queryByText(/403 · proxy block/)).toBeTruthy();
    expect(queryByText(/503 · upstream error/)).toBeTruthy();
    expect(queryByText(/200 ·/)).toBeNull();
  });

  it('omits the timestamp when ts failed to parse (ts === 0)', () => {
    const { container } = render(<EgressLog connected events={[ev({ _id: 1, ts: 0 })]} />);
    // tabular-nums is only on the timestamp span; absent when ts is 0.
    expect(container.querySelector('.tabular-nums')).toBeNull();
  });

  it('surfaces the classification detail on hover, anywhere over the row', () => {
    const { getByText, getByRole } = render(
      <EgressLog
        connected
        events={[ev({ _id: 1, decision: 'TCP_DENIED', status: '403', host: 'evil.example.com' })]}
      />,
    );
    // Hover over the host text — not the status label — to prove the tooltip is
    // row-wide, not pinned to one cell.
    fireEvent.mouseMove(getByText(/evil\.example\.com/), { clientX: 10, clientY: 10 });
    expect(getByRole('tooltip')).toHaveTextContent(/Blocked by the squid proxy/);
  });

  it('renders a Europe/Paris timestamp when ts is present', () => {
    // 2024-06-27T13:33:20Z → 15:33:20 in Europe/Paris (CEST, UTC+2).
    const { container } = render(<EgressLog connected events={[ev({ _id: 1, ts: 1719495200 })]} />);
    const ts = container.querySelector('.tabular-nums');
    expect(ts?.textContent).toBe('15:33:20');
  });
});
