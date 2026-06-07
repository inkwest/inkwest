import { useEffect, useMemo, useState } from 'react';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import * as api from '@/services/api';
import { useConnectorsStore } from '@/store/connectors';
import { AwsAccount, AwsRole } from '@/types/connectors';

interface Props {
  sessionId: string;
  isOpen: boolean;
  onClose: () => void;
}

function key(accountId: string, roleName: string): string {
  return `${accountId}::${roleName}`;
}

// Shown after AWS SSO auth: pick one or MORE account/role pairs (one AWS profile
// each). Grouped by account name, searchable, full height — there can be many.
export function AwsAccountRoleModal({ sessionId, isOpen, onClose }: Props) {
  const selectAwsRoles = useConnectorsStore((s) => s.selectAwsRoles);
  const [accounts, setAccounts] = useState<AwsAccount[]>([]);
  const [roles, setRoles] = useState<AwsRole[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setLoading(true);
    setError(null);
    setSelected(new Set());
    setQuery('');
    api
      .getAwsOptions(sessionId)
      .then((opts) => {
        setAccounts(opts.accounts);
        setRoles(opts.roles);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [isOpen, sessionId]);

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
    setSubmitting(true);
    setError(null);
    try {
      await selectAwsRoles(sessionId, selections);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Sélection des comptes & rôles AWS" size="lg">
      <div className="flex flex-col" style={{ maxHeight: '80vh' }}>
        {/* Search + global check-all */}
        <div className="flex items-center gap-2 mb-3">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Rechercher un compte ou un rôle…"
            className="flex-1 bg-gray-800 text-gray-100 text-sm rounded-md px-3 py-2 border border-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <Button variant="secondary" size="sm" onClick={toggleAllVisible} disabled={visibleKeys.length === 0}>
            {allVisibleSelected ? 'Tout décocher' : 'Tout cocher'}
          </Button>
        </div>

        {loading && <p className="text-gray-400 text-sm">Chargement des comptes…</p>}
        {error && <p className="text-red-400 text-sm">{error}</p>}
        {!loading && grouped.length === 0 && !error && (
          <p className="text-gray-400 text-sm">
            {query ? 'Aucun résultat pour cette recherche.' : 'Aucun compte/rôle disponible pour ton SSO.'}
          </p>
        )}

        <div className="flex-1 overflow-y-auto pr-1 space-y-3">
          {grouped.map(({ account, roles: accountRoles }) => {
            const allOn = accountRoles.every((r) => selected.has(key(account.account_id, r)));
            return (
              <div key={account.account_id} className="rounded-md bg-gray-800/60">
                <button
                  onClick={() => toggleAccount(account.account_id, accountRoles)}
                  className="w-full flex items-center justify-between px-3 py-2 text-left"
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
                          className="accent-blue-600"
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

        <div className="flex items-center justify-between gap-2 pt-3 border-t border-gray-800 mt-2">
          <span className="text-xs text-gray-500">{selected.size} sélectionné(s)</span>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose}>
              Annuler
            </Button>
            <Button onClick={handleConfirm} disabled={selected.size === 0} isLoading={submitting}>
              Donner accès
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
