import { useEffect, useMemo, useRef, useState } from 'react';
import { clsx } from 'clsx';
import { Check, ChevronDown, Image } from 'lucide-react';
import { modelLabel, type AppConfig, type ModelInfo } from '@/services/api';
import { engineLabel } from '@/lib/engines';

/** Whether a model accepts image input (case-insensitive over input_modalities). */
function acceptsImage(m: ModelInfo | undefined): boolean {
  return !!m?.input_modalities?.some((x) => x.toUpperCase() === 'IMAGE');
}

// Highlight the (first) search-match substring so a filtered list shows *why* each row
// matched. Case-insensitive; unchanged when there's no query/match. Mirrors the admin
// ModelSettingsPage helper.
function highlightMatch(text: string, query: string) {
  const q = query.trim();
  const idx = q ? text.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (idx === -1) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-amber-400/30 text-amber-100 rounded px-0.5">{text.slice(idx, idx + q.length)}</mark>
      {text.slice(idx + q.length)}
    </>
  );
}

type Row =
  | { kind: 'header'; key: string; engine: string }
  | {
      kind: 'option';
      key: string;
      model: string | null;
      engine: string | null;
      modelLabel: string;
      withEngine: string | null; // engine prefix to render (null = none)
      image?: boolean; // model accepts image input → show the 🖼️ icon
      disabled?: boolean;
    };

interface Props {
  config: AppConfig;
  /** Currently selected model (null = the "Default" entry). */
  model: string | null;
  /** Currently selected engine (null = unset / resolve per turn). */
  engine: string | null;
  onChange: (model: string | null, engine: string | null) => void;
  /** Extra classes on the relative wrapper. */
  className?: string;
  /** Trigger button classes — lets the chat pill and the full-width form control differ. */
  triggerClassName?: string;
  /** Popover position+width classes; defaults to a fixed-width menu anchored right. Pass
   *  e.g. "left-0 right-0" so the menu matches a full-width trigger. */
  menuClassName?: string;
  title?: string;
  /** Keep a since-disabled pinned model selectable+visible (chat selector only). */
  allowDisabledCurrent?: boolean;
}

/**
 * Controlled Engine·Model picker shared by the chat selector and the new-session
 * screen. When more than one engine is enabled it offers Engine·Model pairs
 * (**ordered engine-first, then model**), with each model appearing under every
 * engine that can run it (Claude → both; other vendors → inkwest-agent). With a
 * single engine it shows plain model names — there's no choice. A type-to-filter
 * box keeps the whole-Bedrock catalog (50+ models) navigable.
 */
export function ModelEnginePicker({
  config,
  model,
  engine,
  onChange,
  className,
  triggerClassName,
  menuClassName,
  title,
  allowDisabledCurrent = false,
}: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const containerRef = useRef<HTMLDivElement>(null);

  // Close on an outside click; Escape first clears a non-empty query, then closes.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  // >1 engine enabled ⇒ Engine·Model pairs; otherwise plain model names.
  const showEngine = config.engines.length > 1;
  const defaultLabel = modelLabel(config.available_models, config.model);

  // The engine the default runs on, resolved authoritatively server-side (admin choice
  // or heuristic) — so the "Default" entry names the exact Engine·Model pairing it runs.
  const defaultEngine = config.default_engine || null;
  const defaultDisplay =
    showEngine && defaultEngine
      ? `${engineLabel(defaultEngine)} · ${defaultLabel}`
      : defaultLabel;

  // Build rows engine-first: the "Default" entry, then per engine a header followed by
  // every model runnable on it. Single-engine ⇒ a flat model list, no headers.
  const rows = useMemo<Row[]>(() => {
    const out: Row[] = [
      { kind: 'option', key: 'default', model: null, engine: null, modelLabel: '', withEngine: null },
    ];
    if (showEngine) {
      for (const eng of config.engines) {
        const models = config.available_models.filter((m) => m.engines.includes(eng));
        if (models.length === 0) continue;
        out.push({ kind: 'header', key: `h:${eng}`, engine: eng });
        for (const m of models) {
          out.push({
            kind: 'option',
            key: `${eng} ${m.id}`,
            model: m.id,
            engine: eng,
            modelLabel: m.label,
            withEngine: eng,
            image: acceptsImage(m),
          });
        }
      }
    } else {
      for (const m of config.available_models) {
        out.push({
          kind: 'option',
          key: m.id,
          model: m.id,
          engine: null,
          modelLabel: m.label,
          withEngine: null,
          image: acceptsImage(m),
        });
      }
    }
    // A session pinned to a since-disabled model stays selectable, marked "(disabled)":
    // the backend ignores the stale pin at turn time and runs the default instead.
    if (allowDisabledCurrent && model && !config.available_models.some((m) => m.id === model)) {
      out.push({
        kind: 'option',
        key: `${model} disabled`,
        model,
        engine,
        modelLabel: `${model} (disabled)`,
        withEngine: null,
        disabled: true,
      });
    }
    return out;
  }, [config, showEngine, model, engine, allowDisabledCurrent]);

  // Filter options by the query (model id/label or engine label), then drop headers
  // whose group went empty. "Default" stays pinned at the top, always reachable.
  const visible = useMemo<Row[]>(() => {
    const q = query.trim().toLowerCase();
    const matched = rows.filter((r) => {
      if (r.kind === 'header') return true;
      if (r.model === null) return true;
      return (
        !q ||
        r.model.toLowerCase().includes(q) ||
        r.modelLabel.toLowerCase().includes(q) ||
        (r.engine ? engineLabel(r.engine).toLowerCase().includes(q) : false)
      );
    });
    return matched.filter((r, i) => {
      if (r.kind !== 'header') return true;
      const next = matched[i + 1];
      return next !== undefined && next.kind === 'option';
    });
  }, [rows, query]);

  // Collapsed trigger stays compact: it shows the resolved pairing (the "Default — "
  // prefix is dropped here to avoid truncation; the dropdown's Default row keeps it).
  const triggerLabel =
    model === null
      ? defaultDisplay
      : showEngine && engine
        ? `${engineLabel(engine)} · ${modelLabel(config.available_models, model)}`
        : modelLabel(config.available_models, model);

  const isCurrent = (r: Extract<Row, { kind: 'option' }>) => {
    // On Default (no pin), check the Default row AND the concrete Engine·Model pairing
    // the default resolves to, so the user sees exactly what will run.
    if (model === null) {
      return r.model === null || (r.model === config.model && r.engine === defaultEngine);
    }
    return r.model === model && (!showEngine || r.engine === engine);
  };

  const choose = (r: Extract<Row, { kind: 'option' }>) => {
    setOpen(false);
    setQuery('');
    onChange(r.model, r.engine);
  };

  return (
    <div ref={containerRef} className={clsx('relative', className)}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={clsx(
          'flex items-center gap-1 cursor-pointer focus:outline-none focus:ring-1 focus:ring-blue-500',
          triggerClassName ??
            'text-xs text-gray-400 bg-gray-800 border border-gray-700 rounded-full px-2.5 py-0.5 max-w-72'
        )}
        title={title}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="truncate flex-1 text-left">{triggerLabel}</span>
        <ChevronDown className="w-3 h-3 shrink-0" />
      </button>

      {open && (
        <div
          className={clsx(
            'absolute z-50 mt-1 max-h-80 flex flex-col rounded-md border border-gray-700 bg-gray-800 shadow-lg',
            menuClassName ?? 'right-0 w-72'
          )}
          role="listbox"
        >
          <div className="p-2 border-b border-gray-700">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  e.stopPropagation();
                  if (query) setQuery('');
                  else setOpen(false);
                }
              }}
              autoFocus
              placeholder="Search models…"
              className="w-full bg-gray-900 text-gray-100 text-xs rounded px-2 py-1 border border-gray-700 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <div className="overflow-y-auto py-1">
            {visible.length === 0 && (
              <p className="px-3 py-2 text-xs text-gray-500">No models match your search.</p>
            )}
            {visible.map((r) =>
              r.kind === 'header' ? (
                <div
                  key={r.key}
                  className="px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-500"
                >
                  {engineLabel(r.engine)}
                </div>
              ) : (
                <button
                  key={r.key}
                  type="button"
                  role="option"
                  aria-selected={isCurrent(r)}
                  onClick={() => choose(r)}
                  className={clsx(
                    'w-full flex items-center gap-2 text-left px-3 py-1.5 text-xs hover:bg-gray-700/70',
                    isCurrent(r) ? 'text-blue-300' : 'text-gray-300'
                  )}
                >
                  <Check className={clsx('w-3 h-3 shrink-0', isCurrent(r) ? 'opacity-100' : 'opacity-0')} />
                  <span className="truncate">
                    {r.model === null ? (
                      <>Default — {defaultDisplay}</>
                    ) : r.withEngine && !r.disabled ? (
                      <>
                        <span className="text-gray-500">
                          {highlightMatch(engineLabel(r.withEngine), query)} ·{' '}
                        </span>
                        {highlightMatch(r.modelLabel, query)}
                      </>
                    ) : (
                      highlightMatch(r.modelLabel, query)
                    )}
                  </span>
                  {r.image && (
                    <Image
                      className="w-3 h-3 shrink-0 text-gray-500"
                      aria-label="IMAGE — image input"
                    >
                      <title>IMAGE — accepts image input (uploads work)</title>
                    </Image>
                  )}
                </button>
              )
            )}
          </div>
        </div>
      )}
    </div>
  );
}
