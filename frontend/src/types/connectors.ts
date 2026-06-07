// External access connector types (mirror of backend ConnectorStatusResponse).

export type ConnectorName = 'aws' | 'gitlab' | 'coralogix';

export type ConnectorState =
  | 'not_connected'
  | 'pending_auth'
  | 'needs_selection'
  | 'connected'
  | 'expired'
  | 'error';

export interface AwsAccount {
  account_id: string;
  account_name: string;
}

export interface AwsRole {
  account_id: string;
  role_name: string;
}

// Never carries a secret — only the lifecycle state and safe metadata.
export interface ConnectorStatus {
  connector: ConnectorName;
  state: ConnectorState;
  expires_at?: string | null;
  scope?: string | null;
  selection?: Record<string, unknown> | null;
  available_accounts?: AwsAccount[] | null;
  available_roles?: AwsRole[] | null;
  error?: string | null;
}
