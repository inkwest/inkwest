import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { ModelUsageTable } from './ModelUsageTable';
import type { ModelUsage } from '@/types/admin';

const DATA: ModelUsage[] = [
  { model: 'claude-opus-4-8', engine: 'claude-cli', input_tokens: 100, output_tokens: 40, total_tokens: 140, cost_usd: 2.5, request_count: 3 },
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

  it('shows an empty-state row when there is no data', () => {
    render(<ModelUsageTable data={[]} />);
    expect(screen.getByText('No usage data for this period')).toBeInTheDocument();
  });
});
