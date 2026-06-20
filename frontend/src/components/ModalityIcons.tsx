import { clsx } from 'clsx';
import { INPUT_MODALITIES, MODALITY_META, hasModality } from '@/lib/modality';

/** Small input-modality icons for a model (TEXT is universal → no icon). Renders nothing
 *  when the model has no badged modality. Each icon carries an ALL-CAPS hover tooltip.
 *  Shared by the admin model list and the chat model picker. */
export function ModalityIcons({
  modalities,
  className,
}: {
  modalities: string[] | undefined;
  className?: string;
}) {
  const present = INPUT_MODALITIES.filter((mod) => hasModality(modalities, mod));
  if (present.length === 0) return null;
  return (
    <span className={clsx('flex items-center gap-1 shrink-0', className)}>
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
