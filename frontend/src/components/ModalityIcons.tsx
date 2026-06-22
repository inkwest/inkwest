import { clsx } from 'clsx';
import { INPUT_MODALITIES, MODALITY_META, TEXT_META, hasModality } from '@/lib/modality';

/** Small input-modality icons for a model. The (T)ext icon is rendered first whenever the
 *  model accepts text input — declared, or unknown/empty (treated as text-only). That gives
 *  the optional icons context (a lone IMAGE icon was misread as "image-only" rather than
 *  "text + image"); a model that declares only non-text input correctly gets no (T) icon.
 *  Renders nothing when there's no modality to show. Each icon carries an ALL-CAPS hover
 *  tooltip. Shared by the admin model list and the chat model picker. */
export function ModalityIcons({
  modalities,
  className,
}: {
  modalities: string[] | undefined;
  className?: string;
}) {
  const present = INPUT_MODALITIES.filter((mod) => hasModality(modalities, mod));
  // Unknown/empty modalities mean text-only (back-compat with catalogs that report nothing).
  const showText = hasModality(modalities, 'TEXT') || !modalities?.length;
  if (!showText && present.length === 0) return null;
  return (
    <span className={clsx('flex items-center gap-1 shrink-0', className)}>
      {showText && (
        <span title={TEXT_META.tip} aria-label={TEXT_META.tip}>
          <TEXT_META.Icon className="w-3.5 h-3.5 text-gray-400" />
        </span>
      )}
      {present.map((mod) => {
        const { Icon, tip } = MODALITY_META[mod];
        return (
          <span key={mod} title={tip} aria-label={tip}>
            <Icon className="w-3.5 h-3.5 text-gray-400" />
          </span>
        );
      })}
    </span>
  );
}
