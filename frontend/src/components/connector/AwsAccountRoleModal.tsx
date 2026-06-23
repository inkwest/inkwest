import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import * as api from '@/services/api';
import { useConnectorsStore } from '@/store/connectors';
import { AwsAccount, AwsRole } from '@/types/connectors';
import { useAwsEnumProgress } from '@/hooks/useAwsEnumProgress';
import { AwsEnumProgressBar } from './AwsEnumProgressBar';

interface Props {
  sessionId: string;
  isOpen: boolean;
  onClose: () => void;
}

function key(accountId: string, roleName: string): string {
  return `${accountId}::${roleName}`;
}

// Minimum time the discovery bar stays visible before auto-close, so a fast grant
// doesn't flash it. No extra wait once discovery has already taken longer than this.
const MIN_PROGRESS_MS = 2000;

// Shown after AWS SSO auth: pick one or MORE account/role pairs (one AWS profile
// each). Grouped by account name, searchable, full height — there can be many.
export function AwsAccountRoleModal({ sessionId, isOpen, onClose }: Props) {
  const selectAwsRoles = useConnectorsStore((s) => s.selectAwsRoles);
  const [accounts, setAccounts] = useState<AwsAccount[]>([]);
  const [roles, setRoles] = useState<AwsRole[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // After granting an eks-* role we stay open to show EKS discovery, then auto-close.
  const [grantWaiting, setGrantWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One bar for every slow phase: SSO refresh (enumerating) and the grant's STS
  // minting / EKS discovery (granting → discovering). `submitting` is included so the
  // minting phase is visible too — with many pairs (e.g. 55) the per-pair "granting"
  // count is the only feedback that the grant is progressing. The backend reports the
  // phase; done/total scopes the active one.
  const progress = useAwsEnumProgress(sessionId, refreshing || submitting || grantWaiting);
  // Arm the discovery-finished close only once we've actually observed progress, so
  // the first (still-empty) poll after a grant can't close the modal prematurely.
  const sawProgress = useRef(false);
  // Timestamp of the first non-idle poll — the moment the bar became visible — so the
  // auto-close can enforce MIN_PROGRESS_MS and avoid a flash on instant discovery.
  const shownAt = useRef(0);
  // Hold onClose in a ref so the auto-close effect doesn't depend on its identity:
  // a parent re-render handing a fresh onClose must not re-run the effect and fire a
  // second close. Kept current every render.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // load(false): cached list (instant); load(true): force a fresh SSO enumeration.
  const load = useCallback(
    async (refresh: boolean) => {
      if (refresh) setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        const opts = refresh
          ? await api.refreshAwsOptions(sessionId)
          : await api.getAwsOptions(sessionId);
        setAccounts(opts.accounts);
        setRoles(opts.roles);
        if (refresh) {
          // Drop checked roles that vanished from the fresh list (revoked AWS-side)
          // — they'd otherwise stay invisibly selected and fail the backend's
          // offered-pair check. Keep every still-present pick.
          const live = new Set(opts.roles.map((r) => key(r.account_id, r.role_name)));
          setSelected((prev) => new Set([...prev].filter((k) => live.has(k))));
        }
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [sessionId]
  );

  useEffect(() => {
    if (!isOpen) return;
    setSelected(new Set());
    setQuery('');
    setGrantWaiting(false);
    sawProgress.current = false;
    shownAt.current = 0;
    void load(false);
  }, [isOpen, sessionId, load]);

  // While waiting on EKS discovery, close once the backend goes idle again — but
  // only after we've seen at least one non-idle poll (sawProgress), so a poll that
  // lands before the entry is published can't trigger an immediate close. Keep the bar
  // up for at least MIN_PROGRESS_MS so a fast grant doesn't flash it; if discovery
  // already ran longer, remaining is 0 and it closes right away. Setting grantWaiting
  // false before closing also makes a re-run a no-op (single close).
  useEffect(() => {
    if (!grantWaiting) return;
    if (progress) {
      if (!sawProgress.current) {
        sawProgress.current = true;
        shownAt.current = Date.now();
      }
      return;
    }
    if (!sawProgress.current) return;
    const remaining = Math.max(0, MIN_PROGRESS_MS - (Date.now() - shownAt.current));
    const t = setTimeout(() => {
      setGrantWaiting(false);
      onCloseRef.current();
    }, remaining);
    return () => clearTimeout(t);
  }, [grantWaiting, progress]);

  // Safety net: never leave the picker hung if discovery never reports idle (backend
  // died, or a cross-replica poll only ever sees "idle" so sawProgress never arms).
  // Discovery is seconds; this just bounds the worst case.
  useEffect(() => {
    if (!grantWaiting) return;
    const t = setTimeout(() => {
      setGrantWaiting(false);
      onCloseRef.current();
    }, 90_000);
    return () => clearTimeout(t);
  }, [grantWaiting]);

  const nameById = useMemo(
    () => Object.fromEntries(accounts.map((a) => [a.account_id, a.account_name])),
    [accounts]
  );

  // Filter by account name / id / role, then group by account (sorted).
  const grouped = useMemo(() => {
    const q = query.trim().toLowerCase();
    const match = (r: AwsRole) =>
      !q ||
      (nameById[r.account_id] || '').toLowerCase().includes(q) ||
      r.account_id.toLowerCase().includes(q) ||
      r.role_name.toLowerCase().includes(q);

    const byId: Record<string, string[]> = {};
    for (const r of roles) if (match(r)) (byId[r.account_id] ||= []).push(r.role_name);
    for (const id of Object.keys(byId)) byId[id].sort((a, b) => a.localeCompare(b));

    return [...accounts]
      .filter((a) => byId[a.account_id]?.length)
      .sort((a, b) => a.account_name.localeCompare(b.account_name))
      .map((a) => ({ account: a, roles: byId[a.account_id] }));
  }, [accounts, roles, query, nameById]);

  // All currently visible (filtered) role keys, for the global check-all toggle.
  const visibleKeys = useMemo(
    () => grouped.flatMap(({ account, roles: rs }) => rs.map((r) => key(account.account_id, r))),
    [grouped]
  );
  const allVisibleSelected = visibleKeys.length > 0 && visibleKeys.every((k) => selected.has(k));

  const toggle = (accountId: string, roleName: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const k = key(accountId, roleName);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const toggleAccount = (accountId: string, accountRoles: string[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const allOn = accountRoles.every((r) => next.has(key(accountId, r)));
      for (const r of accountRoles) {
        const k = key(accountId, r);
        if (allOn) next.delete(k);
        else next.add(k);
      }
      return next;
    });
  };

  const toggleAllVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) visibleKeys.forEach((k) => next.delete(k));
      else visibleKeys.forEach((k) => next.add(k));
      return next;
    });
  };

  const handleConfirm = async () => {
    if (selected.size === 0) return;
    const selections = [...selected].map((k) => {
      const [account_id, role_name] = k.split('::');
      return { account_id, role_name };
    });
    // An eks-* pick triggers EKS discovery the backend tracks; stay open to show it
    // (auto-closing when it settles). Any other pick has nothing to wait on — close
    // immediately, as before. The backend gates the tracked phase on the same prefix.
    const hasEks = selections.some((s) => s.role_name.toLowerCase().startsWith('eks-'));
    setSubmitting(true);
    setError(null);
    try {
      await selectAwsRoles(sessionId, selections);
      if (hasEks) setGrantWaiting(true);
      else onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  // Block dismissal while the grant POST is in flight — the Modal closes on
  // Escape/backdrop (and its ✕) unconditionally, so guarding only the Cancel button
  // would be a false lock that lets the user unmount mid-mint. Once minting is done
  // and we're only watching EKS discovery (grantWaiting), closing is allowed: it just
  // stops watching — discovery keeps running in the background.
  const handleClose = () => {
    if (submitting) return;
    setGrantWaiting(false);
    onClose();
  };

  // align="top": keeps the modal anchored while the filtered list resizes.
  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title="Sélection des comptes & rôles AWS"
      size="lg"
      align="top"
      bodyClassName="flex flex-col overflow-hidden"
    >
      <div className="flex min-h-0 flex-1 flex-col">
        {/* Search + global check-all (pinned header) */}
        <div className="flex items-center gap-2 mb-3 shrink-0">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Rechercher un compte ou un rôle…"
            disabled={submitting}
            className="flex-1 bg-gray-800 text-gray-100 text-sm rounded-md px-3 py-2 border border-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <Button variant="secondary" size="sm" onClick={toggleAllVisible} disabled={visibleKeys.length === 0 || submitting}>
            {allVisibleSelected ? 'Tout décocher' : 'Tout cocher'}
          </Button>
          <button
            type="button"
            onClick={() => void load(true)}
            // grantWaiting too: a refresh mid-discovery would overwrite the shared
            // progress entry and auto-close the modal out from under the wait.
            disabled={loading || refreshing || submitting || grantWaiting}
            title="Rafraîchir la liste des rôles"
            aria-label="Rafraîchir la liste des rôles"
            className="p-2 rounded-md text-gray-400 hover:text-gray-100 hover:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <RefreshCw size={16} className={refreshing ? 'animate-spin' : ''} />
          </button>
        </div>

        {loading && <p className="text-gray-400 text-sm shrink-0">Chargement des comptes…</p>}
        {/* Shown while any phase runs (refresh enumeration, or a grant's minting / EKS
            discovery); header/footer stay pinned, so its appearance just shortens the
            scrolling list — no layout jump. */}
        {progress && (
          <div className="mb-2 shrink-0"><AwsEnumProgressBar progress={progress} /></div>
        )}
        {error && <p className="text-red-400 text-sm shrink-0">{error}</p>}
        {!loading && grouped.length === 0 && !error && (
          <p className="text-gray-400 text-sm">
            {query ? 'Aucun résultat pour cette recherche.' : 'Aucun compte/rôle disponible pour ton SSO.'}
          </p>
        )}

        <div className="flex-1 min-h-0 overflow-y-auto pr-1 space-y-3">
          {grouped.map(({ account, roles: accountRoles }) => {
            const allOn = accountRoles.every((r) => selected.has(key(account.account_id, r)));
            return (
              <div key={account.account_id} className="rounded-md bg-gray-800/60">
                <button
                  onClick={() => toggleAccount(account.account_id, accountRoles)}
                  disabled={submitting}
                  className="w-full flex items-center justify-between px-3 py-2 text-left disabled:opacity-50"
                >
                  <span className="text-sm font-medium text-gray-100">
                    {account.account_name}{' '}
                    <span className="text-gray-500 font-normal">({account.account_id})</span>
                  </span>
                  <span className="text-xs text-blue-400">{allOn ? 'tout décocher' : 'tout cocher'}</span>
                </button>
                <div className="px-3 pb-2 space-y-1">
                  {accountRoles.map((r) => {
                    const k = key(account.account_id, r);
                    const checked = selected.has(k);
                    return (
                      <label
                        key={k}
                        className="flex items-center gap-2 text-sm text-gray-200 cursor-pointer hover:text-white"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggle(account.account_id, r)}
                          disabled={submitting}
                          className="accent-blue-600 disabled:opacity-50"
                        />
                        {r}
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        <div className="flex items-center justify-between gap-2 pt-3 border-t border-gray-800 mt-2 shrink-0">
          <span className="text-xs text-gray-500">
            {submitting
              ? `Création de ${selected.size} accès en cours…`
              : grantWaiting
                ? 'Découverte des clusters EKS en cours…'
                : `${selected.size} sélectionné(s)`}
          </span>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={handleClose} disabled={submitting}>
              {grantWaiting ? 'Fermer' : 'Annuler'}
            </Button>
            <Button
              onClick={handleConfirm}
              disabled={selected.size === 0 || grantWaiting}
              isLoading={submitting}
            >
              Donner accès
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
