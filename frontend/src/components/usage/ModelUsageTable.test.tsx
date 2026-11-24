import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { ModelUsageTable } from './ModelUsageTable';
import type { ModelUsage } from '@/types/admin';

const DATA: ModelUsage[] = [
  { model: 'claude-opus-4-8', engine: 'claude-cli', input_tokens: 100, output_tokens: 40, cache_read_tokens: 27082, cache_write_tokens: 3242, total_tokens: 140, cost_usd: 2.5, request_count: 3 },
  // No cache fields at all — an old cached API payload; must render 0 and sort cleanly.
  { model: 'llama-3', engine: 'inkwest-agent', input_tokens: 10, output_tokens: 5, total_tokens: 15, cost_usd: 0.004, request_count: 1 },
];

function modelCells() {
  // First body cell of each row = model name.
  return screen.getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell')[0].textContent);
}

describe('ModelUsageTable', () => {
  it('renders the engine column and defaults to costliest-first', () => {
    render(<ModelUsageTable data={DATA} />);
    expect(screen.getByText('Engine')).toBeInTheDocument();
    expect(screen.getByText('inkwest-agent')).toBeInTheDocument();
    expect(modelCells()).toEqual(['claude-opus-4-8', 'llama-3']);
  });

  it('tristate-sorts a column on header clicks (asc → desc → server order)', () => {
    render(<ModelUsageTable data={DATA} />);
    const modelHeader = screen.getByText(/^Model/);
    fireEvent.click(modelHeader); // asc by model name
    expect(modelCells()).toEqual(['claude-opus-4-8', 'llama-3']);
    fireEvent.click(modelHeader); // desc
    expect(modelCells()).toEqual(['llama-3', 'claude-opus-4-8']);
    fireEvent.click(modelHeader); // none → back to incoming order
    expect(modelCells()).toEqual(['claude-opus-4-8', 'llama-3']);
  });

  // The two figures of a pair order the rows differently, so the tested order shows which one sorts.
  const PAIR: ModelUsage[] = [
    { model: 'a', engine: 'e', input_tokens: 10, output_tokens: 900, cache_read_tokens: 1, cache_write_tokens: 900, total_tokens: 910, cost_usd: 1, request_count: 1 },
    { model: 'b', engine: 'e', input_tokens: 900, output_tokens: 10, cache_read_tokens: 900, cache_write_tokens: 1, total_tokens: 910, cost_usd: 2, request_count: 1 },
  ];

  it.each([
    [/^In \/ Out/, 'input'],
    [/^Cache R \/ W/, 'cache reads'],
  ])('sorts %s by its first figure (%s)', (name) => {
    render(<ModelUsageTable data={PAIR} />);
    fireEvent.click(screen.getByRole('button', { name }));
    expect(modelCells()).toEqual(['a', 'b']);
  });

  it('shows an empty-state row when there is no data', () => {
    render(<ModelUsageTable data={[]} />);
    expect(screen.getByText('No usage data for this period')).toBeInTheDocument();
  });
});

describe('ModelUsageTable cache column', () => {
  it('renders the combined cache column, 0 / 0 for legacy rows without the fields', () => {
    render(<ModelUsageTable data={DATA} />);
    expect(screen.getByText('Cache R / W')).toBeInTheDocument();
    const firstRow = screen.getAllByRole('row')[1];
    const cells = within(firstRow).getAllByRole('cell').map((c) => c.textContent);
    // model, engine, requests, in / out, tokens, cache (read / write), cost
    expect(cells).toEqual(['claude-opus-4-8', 'claude-cli', '3', '100 / 40', '140', '27.1k / 3.2k', '$2.50']);
    const legacyRow = screen.getAllByRole('row')[2];
    const legacyCells = within(legacyRow).getAllByRole('cell').map((c) => c.textContent);
    expect(legacyCells[5]).toBe('0 / 0');
  });

  it('sorts by the cache column (reads) with legacy-undefined rows coerced to 0', () => {
    render(<ModelUsageTable data={DATA} />);
    fireEvent.click(screen.getByText(/^Cache/));
    // asc: legacy (undefined-as-0) first
    expect(modelCells()).toEqual(['llama-3', 'claude-opus-4-8']);
  });

  it('sorts by the In / Out column (input component)', () => {
    render(<ModelUsageTable data={DATA} />);
    fireEvent.click(screen.getByText(/^In \/ Out/));
    // asc: 10 input before 100
    expect(modelCells()).toEqual(['llama-3', 'claude-opus-4-8']);
  });

  it('spans the empty state across all 7 columns', () => {
    render(<ModelUsageTable data={[]} />);
    expect(screen.getByText('No usage data for this period')).toHaveAttribute('colspan', '7');
  });
});
