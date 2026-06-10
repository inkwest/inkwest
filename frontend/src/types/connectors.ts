// External access connector types (mirror of backend ConnectorStatusResponse).

export type ConnectorName = 'aws' | 'gitlab';

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

// One AWS account/role profile a session previously selected (no secrets).
export interface AwsSelectionProfile {
  name: string;
  account_id: string;
  account_name: string;
  role_name: string;
}

export interface AwsSelection {
  profiles: AwsSelectionProfile[];
}

// A connector the session previously granted, returned by GET /renewable to drive
// the single "Renew access" action.
export interface PriorGrant {
  connector: ConnectorName;
  live: boolean; // a usable (non-expired) credential is currently held
  state: ConnectorState;
  aws_selection?: AwsSelection | null;
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
