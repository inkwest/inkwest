import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConnectorPermissions } from './ConnectorPermissions';
import { ConnectorStatus } from '@/types/connectors';

describe('ConnectorPermissions', () => {
  it('shows "no access" when not connected', () => {
    render(<ConnectorPermissions status={{ connector: 'gitlab', state: 'not_connected' }} />);
    expect(screen.getByText(/no access granted/i)).toBeInTheDocument();
  });

  it('shows the scope label for a connected scope-based connector', () => {
    const status: ConnectorStatus = {
      connector: 'gitlab',
      state: 'connected',
      scope: 'read_api read_repository',
    };
    render(<ConnectorPermissions status={status} />);
    expect(screen.getByText('read_api read_repository')).toBeInTheDocument();
  });

  it('lists AWS account/role profiles when connected', () => {
    const status: ConnectorStatus = {
      connector: 'aws',
      state: 'connected',
      selection: {
        profiles: [
          {
            name: 'acme-RO',
            account_id: '1',
            account_name: 'acme-sandbox',
            role_name: 'AWSReadOnlyAccess',
          },
        ],
      },
    };
    render(<ConnectorPermissions status={status} />);
    expect(screen.getByText(/acme-sandbox/)).toBeInTheDocument();
    expect(screen.getByText(/AWSReadOnlyAccess/)).toBeInTheDocument();
  });

  it('prompts for selection when AWS is authenticated but unselected', () => {
    render(<ConnectorPermissions status={{ connector: 'aws', state: 'needs_selection' }} />);
    expect(screen.getByText(/choose an account\/role/i)).toBeInTheDocument();
  });
});
