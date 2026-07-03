import { create } from 'zustand';
import * as api from '@/services/api';
import { ConnectorName, ConnectorStatus, PriorGrant } from '@/types/connectors';
import { useSessionsStore } from '@/store/sessions';
import { openConnectorAuthPopup } from '@/lib/connectorConnect';
import { centeredPopupFeatures } from '@/lib/popupCenter';

// Grant/revoke/renew persist a 'system' event message server-side; re-fetch the open
// session so it appears inline in the stream. Fire-and-forget (the access change must
// not wait on a UI refresh) and flicker-free — fetchSession only shows the loading
// state on an initial load, not a refresh of the session already open.
function reloadConversation(sessionId: string): void {
  void useSessionsStore.getState().fetchSession(sessionId);
}

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
  // Connector whose grant is currently starting, shared across every surface
  // (header panel + in-conversation notice) so the same grant can't be kicked off
  // twice concurrently from two places.
  grantBusy: ConnectorName | null;

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
  closeAwsVerification: () => void;
  clearAwsDevice: () => void;
  requestAwsModal: () => void;
  clearAwsModalRequest: () => void;
  setGrantBusy: (connector: ConnectorName | null) => void;
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
  // The "no" chrome features + explicit size are what make browsers open a small
  // popup window rather than a full tab. It stays best-effort: some browsers/settings
  // still open a tab regardless. Centred on the browser window (multi-monitor aware).
  const features = [
    centeredPopupFeatures(520, 720),
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
  grantBusy: null,

  fetchStatus: async (sessionId) => {
    set({ isLoading: true, error: null });
    try {
      const list = await api.getConnectorStatus(sessionId);
      const statuses = indexByConnector(list);
      // Clear a pending re-auth prompt once that connector is back to connected,
      // so the in-conversation notice and header highlight resolve on a fresh grant.
      const reauth = get().reauthRequired;
      const resolved = reauth && statuses[reauth]?.state === 'connected';
      set({ statuses, isLoading: false, ...(resolved ? { reauthRequired: null } : {}) });
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
  // silently when its token is still held; if its token is gone it can't refresh
  // silently, so we surface its re-auth prompt and the user reconnects via the popup.
  // AWS re-runs device approval and the poll auto-applies the saved selection.
  //
  // A GitLab popup never navigates away, so it no longer conflicts with an AWS device
  // approval — no deferral. But a popup needs a user gesture, which a renewal running
  // after awaits (or inside the AWS device poll) doesn't have; hence the prompt rather
  // than auto-opening. The prompt is set BEFORE the AWS branch so it survives the device
  // flow and is waiting once AWS settles.
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
        reloadConversation(sessionId); // GitLab "renewed" event now persisted
      } catch {
        gitlabNeedsOAuth = true; // token gone → silent refresh impossible
      }
    }
    // Silent refresh impossible → surface the re-auth prompt; the user clicks to open
    // the popup (gesture). Set now so it persists through any AWS device approval below.
    if (gitlabNeedsOAuth) set({ reauthRequired: 'gitlab' });

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
        await get().fetchStatus(sessionId);
        return;
      }
      set({ awsAutoSelect: true });
      await get().startAwsDevice(sessionId);
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
      // GitLab is granted outright here (→ event persisted); AWS only authenticates
      // (needs_selection) and emits its event at the account/role pick, so skip it.
      if (connector !== 'aws') reloadConversation(sessionId);
      return true;
    } catch {
      return false;
    }
  },

  // OAuth connectors (GitLab) grant in a popup so the main window never navigates: open
  // it (popup-blocker safe), then poll connector status until it's connected, the popup
  // closes, or a deadline elapses. The backend callback self-closes the popup. Mirrors
  // McpPanel.connectOauth. Resolves once the flow settles, so the caller's grant-busy
  // flag stays set ("Waiting for …") for the whole approval.
  initiate: async (sessionId, connector) => {
    const popup = openConnectorAuthPopup(sessionId, connector, () =>
      set({ error: `Failed to start ${connector} authorization` })
    );
    const startedAt = Date.now();
    const POLL_DEADLINE_MS = 5 * 60 * 1000;
    await new Promise<void>((resolve) => {
      const timer = window.setInterval(async () => {
        // Lightweight status poll — no isLoading toggle, so the panel doesn't flicker
        // every 2s; also clears a pending re-auth prompt once this connector lands.
        try {
          const statuses = indexByConnector(await api.getConnectorStatus(sessionId));
          set((s) => ({
            statuses,
            reauthRequired:
              s.reauthRequired === connector && statuses[connector]?.state === 'connected'
                ? null
                : s.reauthRequired,
          }));
        } catch {
          /* transient; keep polling until the popup closes or the deadline */
        }
        const connected = get().statuses[connector]?.state === 'connected';
        if (connected || !popup || popup.closed || Date.now() - startedAt > POLL_DEADLINE_MS) {
          window.clearInterval(timer);
          // Close the popup from the opener once granted — this is the reliable path.
          // The callback's own window.close() is best-effort: browsers block a popup
          // self-closing after the cross-origin OAuth round-trip, and the prod CSP
          // (script-src 'self') blocks the inline script outright. An opener can always
          // close a window it opened (same as the AWS flow's closeAwsPopup).
          if (connected) {
            try {
              popup?.close();
            } catch {
              /* already closed / cross-origin — nothing to do */
            }
          }
          resolve();
        }
      }, 2000);
    });
    // Persist the inline "granted" system event once it actually connected.
    if (get().statuses[connector]?.state === 'connected') reloadConversation(sessionId);
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

  // Close the verification popup as soon as the code is validated — the approving
  // poll then blocks on account enumeration, so we can't wait for it to return.
  // Idempotent (closeAwsPopup no-ops on an already-closed window).
  closeAwsVerification: () => closeAwsPopup(),

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
        reloadConversation(sessionId); // AWS "renewed" event now persisted
        // A GitLab renewal needing OAuth was already surfaced as a reauthRequired prompt
        // in renewAll (its popup needs a user gesture) — nothing to trigger here.
      } catch {
        // Saved selection invalid → manual picker.
        set({ awsDevice: null, awsAutoSelect: false, awsModalRequested: true });
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
    // A GitLab re-auth prompt set alongside this renewal (reauthRequired) is left as-is:
    // cancelling AWS shouldn't drop a GitLab reconnect the user still needs.
    set({ awsDevice: null, awsAutoSelect: false });
  },

  // Ask the (always-mounted) ConnectorPanel to open the AWS account/role picker.
  // Lets the in-conversation notice reuse the same picker without owning the modal.
  requestAwsModal: () => set({ awsModalRequested: true }),

  clearAwsModalRequest: () => set({ awsModalRequested: false }),

  setGrantBusy: (connector) => set({ grantBusy: connector }),

  selectAwsRoles: async (sessionId, selections) => {
    const status = await api.selectAwsRoles(sessionId, selections);
    set((s) => ({
      statuses: { ...s.statuses, aws: status },
      // A completed selection grants AWS — drop any pending re-auth prompt for it.
      reauthRequired: s.reauthRequired === 'aws' ? null : s.reauthRequired,
    }));
    reloadConversation(sessionId); // AWS "granted" event (with account/role) now persisted
  },

  revoke: async (sessionId, connector) => {
    const status = await api.revokeConnector(sessionId, connector);
    set((s) => ({
      statuses: { ...s.statuses, [connector]: status },
      reauthRequired: get().reauthRequired === connector ? null : get().reauthRequired,
    }));
    reloadConversation(sessionId); // "revoked" event now persisted
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
      grantBusy: null,
    }),
}));
