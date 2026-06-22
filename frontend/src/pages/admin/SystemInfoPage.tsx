import { Fragment, useEffect } from 'react';
import { clsx } from 'clsx';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui';
import { useSystemInfoStore } from '@/store/admin';
import { Box, Bot, Settings2, Database } from 'lucide-react';

interface Row {
  label: string;
  value: string;
  mono?: boolean;
  /** Highlight the value as a warning (e.g. a failed catalog fetch). */
  warn?: boolean;
}

/** Definition list of label/value rows. Values sit in a flexible left-aligned column so
 *  long machine-readable refs (image, commit) wrap cleanly instead of raggedly right. */
function InfoList({ rows }: { rows: Row[] }) {
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-x-6 gap-y-3 text-sm">
      {rows.map(({ label, value, mono, warn }) => (
        <Fragment key={label}>
          <dt className="text-gray-400">{label}</dt>
          <dd
            className={clsx(
              'break-all',
              mono && 'font-mono',
              warn ? 'text-amber-400' : !value && 'text-gray-500 italic'
            )}
          >
            {value || 'not configured'}
          </dd>
        </Fragment>
      ))}
    </dl>
  );
}

export function SystemInfoPage() {
  const { info, isLoading, error, fetchSystemInfo } = useSystemInfoStore();

  useEffect(() => {
    fetchSystemInfo();
  }, [fetchSystemInfo]);

  return (
    <div className="space-y-6 max-w-5xl">
      <div>
        <h1 className="text-2xl font-bold">System Info</h1>
        <p className="text-gray-400 mt-1">Running image versions and runtime environment</p>
      </div>

      {error && (
        <div className="rounded-md bg-red-900/30 border border-red-800 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500" />
        </div>
      ) : info ? (
        <div className="space-y-6">
          <Card variant="bordered">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Box className="w-5 h-5 text-blue-400" />
                App image
              </CardTitle>
            </CardHeader>
            <CardContent>
              <InfoList
                rows={[
                  { label: 'Version', value: info.app_version, mono: true },
                  { label: 'Commit', value: info.app_commit, mono: true },
                  { label: 'Built', value: info.app_build_timestamp, mono: true },
                ]}
              />
            </CardContent>
          </Card>

          <Card variant="bordered">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Bot className="w-5 h-5 text-purple-400" />
                Agent image
              </CardTitle>
            </CardHeader>
            <CardContent>
              <InfoList rows={[{ label: 'Image', value: info.agent_image, mono: true }]} />
            </CardContent>
          </Card>

          <Card variant="bordered">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Settings2 className="w-5 h-5 text-green-400" />
                Environment
              </CardTitle>
            </CardHeader>
            <CardContent>
              <InfoList
                rows={[
                  { label: 'Environment', value: info.app_env },
                  { label: 'AI provider', value: info.ai_provider },
                  { label: 'Default model', value: info.default_model, mono: true },
                  { label: 'Engines', value: info.engines.join(', ') },
                  { label: 'Auth mode', value: info.oidc_enabled ? 'OIDC' : 'Dev (auto-login)' },
                  { label: 'Sandbox', value: info.sandbox_enabled ? 'Enabled' : 'Disabled' },
                  {
                    label: 'Attachments',
                    value: info.attachments_enabled ? 'Enabled' : 'Disabled',
                  },
                ]}
              />
            </CardContent>
          </Card>

          <Card variant="bordered">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Database className="w-5 h-5 text-amber-400" />
                Model catalog
              </CardTitle>
            </CardHeader>
            <CardContent>
              <InfoList
                rows={[
                  { label: 'Source', value: info.catalog.source },
                  {
                    label: 'Last refreshed',
                    value: info.catalog.last_refreshed_at ?? 'never',
                    mono: true,
                  },
                  {
                    label: 'Status',
                    value: info.catalog.last_fetch_failed ? 'Last fetch failed' : 'OK',
                    warn: info.catalog.last_fetch_failed,
                  },
                  { label: 'Models', value: String(info.catalog.count) },
                ]}
              />
            </CardContent>
          </Card>
        </div>
      ) : null}
    </div>
  );
}
