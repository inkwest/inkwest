import { create } from 'zustand';
import * as api from '@/services/api';
import { ConnectorName, ConnectorStatus, PriorGrant } from '@/types/connectors';

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
  // Connectors this session previously granted (for the "Renew access" action).
  renewable: PriorGrant[];
  // True during a renew: the AWS device poll should auto-apply the saved selection
  // (skip the manual picker) instead of opening the account/role modal.
  awsAutoSelect: boolean;
  // True when a GitLab OAuth redirect is queued behind an in-progress AWS device
  // approval; fired once AWS completes (deferred so it doesn't abandon AWS).
  gitlabReauthPending: boolean;

  fetchStatus: (sessionId: string) => Promise<void>;
  fetchRenewable: (sessionId: string) => Promise<void>;
  renewAll: (sessionId: string) => Promise<void>;
  tryWarmGrant: (
    sessionId: string,
    connector: ConnectorName,
    opts?: { openPicker?: boolean }
  ) => Promise<boolean>;
  initiate: (sessionId: string, connector: ConnectorName) => Promise<void>;
  startAwsDevice: (sessionId: string) => Promise<void>;
  pollAwsDevice: (sessionId: string) => Promise<string>;
  openAwsVerification: () => void;
  clearAwsDevice: () => void;
  clearAwsModalRequest: () => void;
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
  renewable: [],
  awsAutoSelect: false,
  gitlabReauthPending: false,

  fetchStatus: async (sessionId) => {
    set({ isLoading: true, error: null });
    try {
      const list = await api.getConnectorStatus(sessionId);
      set({ statuses: indexByConnector(list), isLoading: false });
    } catch (e) {
      set({ error: (e as Error).message, isLoading: false });
    }
  },

  // Load the connectors this session previously granted (best-effort).
  fetchRenewable: async (sessionId) => {
    try {
      set({ renewable: await api.getRenewable(sessionId) });
    } catch {
      set({ renewable: [] });
    }
  },

  // Re-grant exactly the previously-granted set (never adds new). GitLab refreshes
  // silently when its token is still held, otherwise it must re-authorize via an
  // OAuth redirect; AWS re-runs device approval and the poll auto-applies the saved
  // selection.
  //
  // GitLab's redirect navigates away, so it can't run alongside an AWS device
  // approval. When both need it, AWS goes first (stays on the page) and the GitLab
  // redirect is deferred until AWS completes (see pollAwsDevice) — by then AWS is
  // connected and persisted, so navigating away is safe. When AWS isn't renewing,
  // GitLab redirects right away.
  renewAll: async (sessionId) => {
    await get().fetchRenewable(sessionId);
    // Only renew connectors that aren't already connected (mirrors the button gate),
    // so renewing one doesn't needlessly re-auth a healthy one — e.g. re-popping the
    // AWS device approval just to renew GitLab.
    const { statuses, renewable } = get();
    const granted = new Set(
      renewable
        .filter((g) => statuses[g.connector]?.state !== 'connected')
        .map((g) => g.connector)
    );
    const willRenewAws = granted.has('aws');

    let gitlabNeedsOAuth = false;
    if (granted.has('gitlab')) {
      try {
        const status = await api.gitlabRenew(sessionId);
        set((s) => ({ statuses: { ...s.statuses, gitlab: status } }));
      } catch {
        gitlabNeedsOAuth = true; // token gone → silent refresh impossible
      }
    }

    if (willRenewAws) {
      // Warm SSO token (user logged in to AWS earlier today) → skip the device
      // approval entirely: provisional from warm + auto-apply the saved selection.
      if (await get().tryWarmGrant(sessionId, 'aws', { openPicker: false })) {
        try {
          const aws = await api.awsRenewSelect(sessionId);
          set((s) => ({ statuses: { ...s.statuses, aws } }));
        } catch {
          set({ awsModalRequested: true }); // saved selection invalid → manual picker
        }
        if (gitlabNeedsOAuth) {
          await get().initiate(sessionId, 'gitlab'); // redirect — navigates away
          return;
        }
        await get().fetchStatus(sessionId);
        return;
      }
      // Defer any GitLab redirect until AWS device approval finishes.
      set({ awsAutoSelect: true, gitlabReauthPending: gitlabNeedsOAuth });
      await get().startAwsDevice(sessionId);
      return;
    }

    if (gitlabNeedsOAuth) {
      await get().initiate(sessionId, 'gitlab'); // OAuth redirect — navigates away
      return;
    }

    await get().fetchStatus(sessionId);
  },

  // Grant from the user's warm platform auth (no login flow). True when granted:
  // AWS lands on needs_selection and the picker opens; GitLab lands connected.
  // False (409 or error) → the caller runs the regular login flow.
  tryWarmGrant: async (sessionId, connector, opts) => {
    try {
      const status = await api.grantFromWarm(sessionId, connector);
      // openPicker=false: renewal auto-applies the saved selection, so the manual
      // picker must not flash open between the grant and the auto-select.
      const openPicker = opts?.openPicker ?? true;
      set((s) => ({
        statuses: { ...s.statuses, [connector]: status },
        ...(connector === 'aws' && status.state === 'needs_selection' && openPicker
          ? { awsModalRequested: true }
          : {}),
      }));
      return true;
    } catch {
      return false;
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
    closeAwsPopup();
    // Renewal: re-apply the previously-saved account/role selection automatically
    // instead of opening the manual picker. Falls back to the picker if the saved
    // selection is no longer valid (backend 400).
    if (status === 'needs_selection' && get().awsAutoSelect) {
      try {
        const aws = await api.awsRenewSelect(sessionId);
        set((s) => ({ statuses: { ...s.statuses, aws }, awsDevice: null, awsAutoSelect: false }));
        await get().fetchStatus(sessionId);
        // AWS is now connected & persisted, so it's safe to navigate away to renew
        // GitLab (a redirect we deferred until the AWS device approval completed).
        if (get().gitlabReauthPending) {
          set({ gitlabReauthPending: false });
          await get().initiate(sessionId, 'gitlab');
        }
      } catch {
        // Saved selection invalid → manual picker. Surface a deferred GitLab renewal
        // as a re-auth prompt the user handles after the manual AWS pick.
        set((s) => ({
          awsDevice: null,
          awsAutoSelect: false,
          awsModalRequested: true,
          reauthRequired: s.gitlabReauthPending ? 'gitlab' : s.reauthRequired,
          gitlabReauthPending: false,
        }));
      }
      return status;
    }
    // needs_selection (or connected) → refresh status, close popup, open the modal.
    await get().fetchStatus(sessionId);
    set({ awsDevice: null, awsModalRequested: status === 'needs_selection' });
    return status;
  },

  clearAwsDevice: () => {
    closeAwsPopup();
    // If a GitLab redirect was queued behind AWS and the user cancels AWS, surface
    // GitLab for a manual reconnect instead of silently dropping it.
    set((s) => ({
      awsDevice: null,
      awsAutoSelect: false,
      reauthRequired: s.gitlabReauthPending ? 'gitlab' : s.reauthRequired,
      gitlabReauthPending: false,
    }));
  },

  clearAwsModalRequest: () => set({ awsModalRequested: false }),

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
      renewable: [],
      awsAutoSelect: false,
      gitlabReauthPending: false,
    }),
}));
