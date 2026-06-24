import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AwsEnumProgressBar } from './AwsEnumProgressBar';

describe('AwsEnumProgressBar', () => {
  it('labels each phase and shows done/total', () => {
    const { rerender } = render(
      <AwsEnumProgressBar progress={{ phase: 'enumerating', done: 3, total: 10 }} />
    );
    expect(screen.getByText(/Récupération de vos comptes AWS… 3\/10/)).toBeInTheDocument();

    rerender(<AwsEnumProgressBar progress={{ phase: 'granting', done: 1, total: 4 }} />);
    expect(screen.getByText(/Création des accès… 1\/4/)).toBeInTheDocument();

    rerender(<AwsEnumProgressBar progress={{ phase: 'discovering', done: 2, total: 5 }} />);
    expect(screen.getByText(/Découverte des clusters EKS… 2\/5/)).toBeInTheDocument();

    // Terminal "done" reads as complete for the frame(s) before the picker auto-closes.
    rerender(<AwsEnumProgressBar progress={{ phase: 'done', done: 3, total: 3 }} />);
    expect(screen.getByText(/Clusters EKS découverts 3\/3/)).toBeInTheDocument();
  });

  it('shows an indeterminate "?" total when total is 0', () => {
    render(<AwsEnumProgressBar progress={{ phase: 'discovering', done: 0, total: 0 }} />);
    expect(screen.getByText(/Découverte des clusters EKS… 0\/\?/)).toBeInTheDocument();
  });

  it('falls back to a neutral label for an unknown phase', () => {
    render(<AwsEnumProgressBar progress={{ phase: 'weird', done: 1, total: 2 }} />);
    expect(screen.getByText(/Traitement en cours… 1\/2/)).toBeInTheDocument();
  });

  it('renders no caption when progress is null', () => {
    render(<AwsEnumProgressBar progress={null} />);
    expect(screen.queryByText(/\d+\/\d+/)).not.toBeInTheDocument();
  });
});
