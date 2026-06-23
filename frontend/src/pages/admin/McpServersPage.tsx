import { useEffect, useState } from 'react';
import { Plus, Edit2, Trash2, ChevronRight, ChevronDown, Copy, Lock } from 'lucide-react';
import {
  Button,
  Card,
  CardContent,
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
  Badge,
  Modal,
  Input,
} from '@/components/ui';
import { useMcpServerStore } from '@/store/admin';
import {
  McpOauthServer,
  CreateMcpOauthServerRequest,
  UpdateMcpOauthServerRequest,
} from '@/types/admin';

// How the OAuth client authenticates to the server. Drives which fields the form shows;
// the backend infers nothing — it just stores client_id/client_secret (both nullable).
type AuthType = 'dcr' | 'public' | 'confidential';

const AUTH_TYPES: { value: AuthType; label: string; hint: string }[] = [
  {
    value: 'dcr',
    label: 'Dynamic (DCR)',
    hint: 'Client is registered automatically at connect time (RFC 7591). No credentials needed.',
  },
  {
    value: 'public',
    label: 'Public',
    hint: 'Pre-registered public client (PKCE, no secret). Requires a client ID.',
  },
  {
    value: 'confidential',
    label: 'Confidential',
    hint: 'Pre-registered client with a secret. Requires a client ID and a client secret.',
  },
];

/** Infer the auth type from a stored server: no client_id ⇒ DCR; id without a secret ⇒
 *  public; id with a secret ⇒ confidential. */
function deriveAuthType(server: McpOauthServer): AuthType {
  if (!server.client_id) return 'dcr';
  return server.has_secret ? 'confidential' : 'public';
}

interface FormState {
  key: string;
  display_name: string;
  url: string;
  scopes: string; // comma-separated in the form
  auth_type: AuthType;
  client_id: string;
  client_secret: string; // blank on edit = keep the stored one
  enabled: boolean;
}

// Shell command an operator runs to mint a SecretBox key (shown when secret storage is off).
const GENERATE_KEY_CMD = 'openssl rand -base64 32';

const EMPTY_FORM: FormState = {
  key: '',
  display_name: '',
  url: '',
  scopes: '',
  auth_type: 'dcr',
  client_id: '',
  client_secret: '',
  enabled: true,
};

export function McpServersPage() {
  const {
    mcpServers,
    secretStorageEnabled,
    isLoading,
    error,
    fetchMcpServers,
    createMcpServer,
    updateMcpServer,
    deleteMcpServer,
    clearError,
  } = useMcpServerStore();

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editing, setEditing] = useState<McpOauthServer | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<{ key?: string; url?: string }>({});

  useEffect(() => {
    fetchMcpServers();
  }, [fetchMcpServers]);

  const handleOpenModal = (server?: McpOauthServer) => {
    if (server) {
      setEditing(server);
      setForm({
        key: server.key,
        display_name: server.display_name || '',
        url: server.url,
        scopes: server.scopes.join(', '),
        auth_type: deriveAuthType(server),
        client_id: server.client_id || '',
        client_secret: '', // never returned; blank = keep existing
        enabled: server.enabled,
      });
      // Surface the Advanced section up front only when it actually holds something.
      setShowAdvanced(!!server.display_name || server.scopes.length > 0);
    } else {
      setEditing(null);
      setForm(EMPTY_FORM);
      setShowAdvanced(false);
    }
    setFieldErrors({});
    clearError();
    setIsModalOpen(true);
  };

  const handleCloseModal = () => {
    setIsModalOpen(false);
    setEditing(null);
    setShowAdvanced(false);
    setFieldErrors({});
    clearError();
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // Can't store a NEW confidential secret without server-side secret storage — the form
    // shows how to enable it instead of the inputs, so there's nothing to submit. An existing
    // confidential server (already has a stored secret) stays editable, secret preserved.
    if (form.auth_type === 'confidential' && !secretStorageEnabled && !editing?.has_secret) return;
    // Pre-submit validation — surface problems here instead of as a server 400.
    const errors: { key?: string; url?: string } = {};
    if (!editing) {
      if (!form.key.trim()) {
        errors.key = 'Key is required.';
      } else if (mcpServers.some((s) => s.key === form.key.trim())) {
        errors.key = 'A server with this key already exists.';
      }
    }
    if (!/^https:\/\//.test(form.url.trim())) {
      errors.url = 'URL must start with https://';
    }
    if (errors.key || errors.url) {
      setFieldErrors(errors);
      return;
    }
    setFieldErrors({});

    const scopes = form.scopes
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    // The auth type decides which credential fields apply; DCR/public must not carry a
    // client_id/secret that no longer applies.
    const clientId = form.auth_type === 'dcr' ? null : form.client_id || null;
    try {
      if (editing) {
        const data: UpdateMcpOauthServerRequest = {
          url: form.url,
          display_name: form.display_name || null,
          scopes,
          client_id: clientId,
          enabled: form.enabled,
        };
        if (form.auth_type === 'confidential') {
          // Send the typed secret; blank preserves the stored one (it's never returned).
          if (form.client_secret) data.client_secret = form.client_secret;
        } else {
          // DCR/public have no secret — clear any stored one ("" = clear, vs null = keep).
          data.client_secret = '';
        }
        await updateMcpServer(editing.key, data);
      } else {
        const data: CreateMcpOauthServerRequest = {
          key: form.key,
          url: form.url,
          display_name: form.display_name || null,
          scopes,
          client_id: clientId,
          client_secret:
            form.auth_type === 'confidential' ? form.client_secret || null : null,
          enabled: form.enabled,
        };
        await createMcpServer(data);
      }
      handleCloseModal();
    } catch {
      // Surfaced by the store's error state.
    }
  };

  const handleDelete = async (key: string) => {
    if (confirm(`Delete the MCP server "${key}"? This cannot be undone.`)) {
      await deleteMcpServer(key);
    }
  };

  const handleToggleEnabled = async (server: McpOauthServer) => {
    await updateMcpServer(server.key, {
      url: server.url,
      display_name: server.display_name,
      scopes: server.scopes,
      client_id: server.client_id,
      enabled: !server.enabled,
    });
  };

  // Confidential picked but the server can't store secrets → show how to enable it instead
  // of the credential inputs, and block submit. An EXISTING confidential server (already has
  // a stored secret) stays editable — its other fields can change and the secret is preserved
  // — so only block when we'd actually need to store a new secret.
  const editingExistingConfidential = !!editing?.has_secret;
  const confidentialUnavailable =
    form.auth_type === 'confidential' && !secretStorageEnabled && !editingExistingConfidential;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">MCP Servers</h1>
          <p className="text-gray-400 mt-1">
            OAuth2 MCP servers users may connect to per conversation
          </p>
        </div>
        <Button onClick={() => handleOpenModal()}>
          <Plus className="w-4 h-4 mr-2" />
          Add MCP Server
        </Button>
      </div>

      {error && (
        <div className="bg-red-900/50 border border-red-700 text-red-400 px-4 py-3 rounded">
          {error}
        </div>
      )}

      <Card variant="bordered">
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Key</TableHead>
                  <TableHead>URL</TableHead>
                  <TableHead>Scopes</TableHead>
                  <TableHead>Auth</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {mcpServers.map((server) => (
                  <TableRow key={server.key}>
                    <TableCell>
                      <div>
                        <p className="font-medium">{server.key}</p>
                        {server.display_name && (
                          <p className="text-sm text-gray-400">{server.display_name}</p>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm text-gray-300 break-all">{server.url}</span>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {server.scopes.length > 0 ? (
                          server.scopes.map((s) => (
                            <Badge key={s} variant="info">
                              {s}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-gray-500">default</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={server.has_secret ? 'info' : 'default'}>
                        {AUTH_TYPES.find((t) => t.value === deriveAuthType(server))?.label}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <button
                        onClick={() => handleToggleEnabled(server)}
                        className={`text-xs px-2 py-1 rounded cursor-pointer ${
                          server.enabled
                            ? 'bg-green-900/50 text-green-400'
                            : 'bg-red-900/50 text-red-400'
                        }`}
                      >
                        {server.enabled ? 'Enabled' : 'Disabled'}
                      </button>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Edit ${server.key}`}
                          disabled={!server.editable}
                          title={
                            server.editable
                              ? undefined
                              : 'Secret can’t be decrypted (the key that sealed it is missing ' +
                                'from SECRETBOX_KEYS). Delete and recreate this server.'
                          }
                          onClick={() => handleOpenModal(server)}
                        >
                          <Edit2 className="w-4 h-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Delete ${server.key}`}
                          onClick={() => handleDelete(server.key)}
                        >
                          <Trash2 className="w-4 h-4 text-red-400" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
                {mcpServers.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center py-8 text-gray-400">
                      No MCP servers configured. Click "Add MCP Server" to create one.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Modal
        isOpen={isModalOpen}
        onClose={handleCloseModal}
        title={editing ? 'Edit MCP Server' : 'Add MCP Server'}
        size="lg"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <Input
            label="Key"
            value={form.key}
            onChange={(e) => setForm({ ...form, key: e.target.value })}
            required
            disabled={!!editing}
            error={fieldErrors.key}
            helperText="Stable identifier users select (also the mcpServers key)"
          />

          <Input
            label="URL"
            value={form.url}
            onChange={(e) => {
              setForm({ ...form, url: e.target.value });
              if (fieldErrors.url) setFieldErrors({ ...fieldErrors, url: undefined });
            }}
            onBlur={() =>
              setFieldErrors((prev) => ({
                ...prev,
                url:
                  form.url.trim() && !/^https:\/\//.test(form.url.trim())
                    ? 'URL must start with https://'
                    : undefined,
              }))
            }
            required
            placeholder="https://mcp.example.com/mcp"
            error={fieldErrors.url}
            helperText="Streamable-HTTP MCP endpoint (must be https)"
          />

          <div>
            <p className="text-sm font-medium text-gray-300 mb-2">Authentication</p>
            <div className="flex rounded-lg border border-gray-700 overflow-hidden">
              {AUTH_TYPES.map((t) => {
                // Confidential needs server-side secret storage. When it's off we grey the
                // option but keep it CLICKABLE — selecting it shows how to enable it (below)
                // instead of the credential inputs.
                const greyed = t.value === 'confidential' && !secretStorageEnabled;
                return (
                  <button
                    key={t.value}
                    type="button"
                    onClick={() => setForm({ ...form, auth_type: t.value })}
                    className={`flex flex-1 items-center justify-center gap-1 px-3 py-2 text-sm font-medium transition-colors ${
                      form.auth_type === t.value
                        ? 'bg-blue-600 text-white'
                        : greyed
                          ? 'bg-gray-800 text-gray-600 hover:bg-gray-700'
                          : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
                    }`}
                  >
                    {greyed && <Lock className="w-3 h-3" />}
                    {t.label}
                  </button>
                );
              })}
            </div>
            <p className="text-sm text-gray-500 mt-1">
              {AUTH_TYPES.find((t) => t.value === form.auth_type)?.hint}
            </p>
          </div>

          {confidentialUnavailable ? (
            // Confidential picked but the server can't store secrets — explain how to enable
            // it (selectable + copyable) in place of the credential inputs.
            <div className="rounded border border-amber-800 bg-amber-900/20 px-3 py-3 text-sm text-amber-200 space-y-2">
              <p>
                Confidential clients need at-rest secret storage, which is not configured on
                this server. Set the <code className="select-all">SECRETBOX_KEYS</code>{' '}
                environment variable (then restart the backend). Generate a key:
              </p>
              <div className="flex items-center gap-2">
                <code className="select-all flex-1 rounded bg-gray-900 px-2 py-1 text-xs text-gray-200">
                  {GENERATE_KEY_CMD}
                </code>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label="Copy command"
                  onClick={() => navigator.clipboard?.writeText(GENERATE_KEY_CMD)}
                >
                  <Copy className="w-4 h-4" />
                </Button>
              </div>
              <p className="text-xs text-amber-300/80">
                Then set <code className="select-all">SECRETBOX_KEYS={'{"1":"<key>"}'}</code>.
              </p>
            </div>
          ) : (
            <>
              {form.auth_type !== 'dcr' && (
                <Input
                  label="Client ID"
                  value={form.client_id}
                  onChange={(e) => setForm({ ...form, client_id: e.target.value })}
                  required
                  helperText="The OAuth client identifier registered with the server"
                />
              )}

              {form.auth_type === 'confidential' &&
                (() => {
                  // Storage off + already-confidential: other fields stay editable, but the
                  // secret can't be changed (no key to seal it) — lock it to preserve-only.
                  const secretLocked = !secretStorageEnabled && editingExistingConfidential;
                  return (
                    <Input
                      label="Client Secret"
                      type="password"
                      value={form.client_secret}
                      onChange={(e) => setForm({ ...form, client_secret: e.target.value })}
                      disabled={secretLocked}
                      placeholder={editing && editing.has_secret ? '•••••••• (unchanged)' : ''}
                      required={!secretLocked && (!editing || !editing.has_secret)}
                      helperText={
                        secretLocked
                          ? 'Stored secret kept. Configure SECRETBOX_KEYS to change it.'
                          : editing && editing.has_secret
                            ? 'Leave blank to keep the stored secret.'
                            : 'The OAuth client secret (stored server-side, never shown again).'
                      }
                    />
                  );
                })()}
            </>
          )}

          <div className="border-t border-gray-700 pt-2">
            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              className="flex items-center gap-1 text-sm text-gray-400 hover:text-gray-200"
            >
              {showAdvanced ? (
                <ChevronDown className="w-4 h-4" />
              ) : (
                <ChevronRight className="w-4 h-4" />
              )}
              Advanced
            </button>
            {showAdvanced && (
              <div className="space-y-4 mt-3">
                <Input
                  label="Display Name"
                  value={form.display_name}
                  onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                  helperText="Optional label shown in the UI; defaults to the key"
                />
                <Input
                  label="Scopes"
                  value={form.scopes}
                  onChange={(e) => setForm({ ...form, scopes: e.target.value })}
                  helperText="Comma-separated OAuth scopes. Empty = the server's default set."
                />
              </div>
            )}
          </div>

          <label className="flex items-center gap-3 p-2 rounded hover:bg-gray-700 cursor-pointer">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
              className="w-4 h-4 rounded border-gray-600 bg-gray-700 text-blue-600 focus:ring-blue-500"
            />
            <span>Enabled (selectable in conversations)</span>
          </label>

          <div className="flex justify-end gap-3 pt-4">
            <Button type="button" variant="secondary" onClick={handleCloseModal}>
              Cancel
            </Button>
            <Button type="submit" disabled={confidentialUnavailable}>
              {editing ? 'Update' : 'Create'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
