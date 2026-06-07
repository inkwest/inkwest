import { create } from 'zustand';
import * as api from '@/services/api';
import { ConnectorName, ConnectorStatus } from '@/types/connectors';

// AWS device-authorization prompt shown while the user approves in their browser.
export interface AwsDeviceState {
  userCode: string;
  verificationUri: string;
  status: 'pending' | 'needs_selection' | 'error';
  detail?: string | null;
}

interface ConnectorsState {
  // Status per connector for the active session.
  statuses: Record<string, ConnectorStatus>;
  isLoading: boolean;
  error: string | null;
  // Connector that needs (re-)auth, surfaced by a connector_required WS event.
  reauthRequired: ConnectorName | null;
  // AWS device flow in progress (code + verification URL), or null.
  awsDevice: AwsDeviceState | null;
  // Set true once device auth is approved → the panel opens the selection modal.
  // Avoids auto-opening on every session load from a stale status.
  awsModalRequested: boolean;

  fetchStatus: (sessionId: string) => Promise<void>;
  initiate: (sessionId: string, connector: ConnectorName) => Promise<void>;
  startAwsDevice: (sessionId: string) => Promise<void>;
  pollAwsDevice: (sessionId: string) => Promise<string>;
  openAwsVerification: () => void;
  clearAwsDevice: () => void;
  clearAwsModalRequest: () => void;
  activateCoralogix: (sessionId: string) => Promise<void>;
  selectAwsRoles: (
    sessionId: string,
    selections: { account_id: string; role_name: string }[]
  ) => Promise<void>;
  revoke: (sessionId: string, connector: ConnectorName) => Promise<void>;
  setReauthRequired: (connector: ConnectorName | null) => void;
  reset: () => void;
}

function indexByConnector(list: ConnectorStatus[]): Record<string, ConnectorStatus> {
  return Object.fromEntries(list.map((s) => [s.connector, s]));
}

// Handle to the small AWS verification popup, so we can close it once approved.
let awsPopup: Window | null = null;

function openAwsPopup(url: string): void {
  const w = 520;
  const h = 720;
  const left = Math.max(0, Math.round((window.screen.width - w) / 2));
  const top = Math.max(0, Math.round((window.screen.height - h) / 2));
  // The "no" chrome features + explicit size are what make browsers open a small
  // popup window rather than a full tab. It stays best-effort: some browsers/settings
  // still open a tab regardless.
  const features = [
    `width=${w}`,
    `height=${h}`,
    `left=${left}`,
    `top=${top}`,
    'menubar=no',
    'toolbar=no',
    'location=no',
    'status=no',
    'resizable=yes',
    'scrollbars=yes',
  ].join(',');
  awsPopup = window.open(url, 'aws-device-auth', features);
}

function closeAwsPopup(): void {
  try {
    awsPopup?.close();
  } catch {
    // ignore cross-origin/closed-window errors
  }
  awsPopup = null;
}

export const useConnectorsStore = create<ConnectorsState>((set, get) => ({
  statuses: {},
  isLoading: false,
  error: null,
  reauthRequired: null,
  awsDevice: null,
  awsModalRequested: false,

  fetchStatus: async (sessionId) => {
    set({ isLoading: true, error: null });
    try {
      const list = await api.getConnectorStatus(sessionId);
      set({ statuses: indexByConnector(list), isLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, isLoading: false });
    }
  },

  // OAuth redirect connectors (GitLab). On return the callback page refreshes status.
  initiate: async (sessionId, connector) => {
    // Remember where to return after the IdP round-trip.
    sessionStorage.setItem('connector_return_to', `/session/${sessionId}`);
    const { redirect_url } = await api.initiateConnector(sessionId, connector);
    window.location.href = redirect_url;
  },

  // AWS uses the device grant: get a code + URL, open it, then poll until approved.
  startAwsDevice: async (sessionId) => {
    const d = await api.startAwsDevice(sessionId);
    set({
      awsDevice: {
        userCode: d.user_code,
        verificationUri: d.verification_uri_complete,
        status: 'pending',
      },
    });
  },

  // User-initiated open of the AWS verification page (best-effort small popup;
  // some embedded browsers open it as a tab — that's out of our control).
  openAwsVerification: () => {
    const d = get().awsDevice;
    if (d) openAwsPopup(d.verificationUri);
  },

  pollAwsDevice: async (sessionId) => {
    const { status, detail } = await api.pollAwsDevice(sessionId);
    if (status === 'pending') return status;
    if (status === 'error') {
      closeAwsPopup();
      set((s) => (s.awsDevice ? { awsDevice: { ...s.awsDevice, status: 'error', detail } } : {}));
      return status;
    }
    // needs_selection (or connected) → refresh status, close popup, open the modal.
    closeAwsPopup();
    await get().fetchStatus(sessionId);
    set({ awsDevice: null, awsModalRequested: status === 'needs_selection' });
    return status;
  },

  clearAwsDevice: () => {
    closeAwsPopup();
    set({ awsDevice: null });
  },

  clearAwsModalRequest: () => set({ awsModalRequested: false }),

  activateCoralogix: async (sessionId) => {
    const status = await api.activateCoralogix(sessionId);
    set((s) => ({ statuses: { ...s.statuses, coralogix: status } }));
  },

  selectAwsRoles: async (sessionId, selections) => {
    const status = await api.selectAwsRoles(sessionId, selections);
    set((s) => ({ statuses: { ...s.statuses, aws: status } }));
  },

  revoke: async (sessionId, connector) => {
    const status = await api.revokeConnector(sessionId, connector);
    set((s) => ({
      statuses: { ...s.statuses, [connector]: status },
      reauthRequired: get().reauthRequired === connector ? null : get().reauthRequired,
    }));
  },

  setReauthRequired: (connector) => set({ reauthRequired: connector }),

  reset: () =>
    set({
      statuses: {},
      error: null,
      reauthRequired: null,
      awsDevice: null,
      awsModalRequested: false,
    }),
}));
