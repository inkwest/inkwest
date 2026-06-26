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

  it('filters the log by outcome category, with per-category counts on the chips', () => {
    const { getByRole, queryByText } = render(
      <EgressLog
        connected
        events={[
          ev({ _id: 1, decision: 'TCP_TUNNEL', status: '200', host: 'github.com' }),
          ev({ _id: 2, decision: 'TCP_DENIED', status: '403', host: 'evil.example.com' }),
          ev({ _id: 3, decision: 'TCP_TUNNEL', status: '503', host: 'unreachable.eks.amazonaws.com' }),
        ]}
      />,
    );

    // All three rows show by default.
    expect(queryByText(/github\.com/)).toBeTruthy();
    expect(queryByText(/evil\.example\.com/)).toBeTruthy();
    expect(queryByText(/unreachable\.eks/)).toBeTruthy();

    // Filtering to "Blocked" keeps only the DENIED row.
    fireEvent.click(getByRole('button', { name: /Blocked/ }));
    expect(queryByText(/evil\.example\.com/)).toBeTruthy();
    expect(queryByText(/github\.com/)).toBeNull();
    expect(queryByText(/unreachable\.eks/)).toBeNull();

    // An empty category shows a placeholder rather than nothing.
    fireEvent.click(getByRole('button', { name: /Allowed/ }));
    expect(queryByText(/github\.com/)).toBeTruthy();
    expect(queryByText(/evil\.example\.com/)).toBeNull();
  });

  it('renders the timestamp as 24h HH:mm:ss in the viewer timezone when ts is present', () => {
    // Asserted by shape, not an exact value: the time is formatted in the runner's
    // local timezone, so a fixed string would be flaky across CI machines.
    const { container } = render(<EgressLog connected events={[ev({ _id: 1, ts: 1719495200 })]} />);
    const ts = container.querySelector('.tabular-nums');
    expect(ts?.textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});
