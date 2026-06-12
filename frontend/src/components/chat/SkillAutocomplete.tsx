import { Skill } from '@/types/api';
import { cn } from '@/lib/utils';
import { formatArgs } from '@/lib/skillArgs';

interface Props {
  matches: Skill[];
  activeIndex: number;
  onSelect: (template: Skill) => void;
  onHover: (index: number) => void;
}

/**
 * Dropdown for the inline "/" skill trigger in the chat composer. Purely
 * presentational — selection/keyboard state lives in ChatInput. Anchored above the
 * textarea (bottom-full) so it never pushes the composer.
 */
export function SkillAutocomplete({ matches, activeIndex, onSelect, onHover }: Props) {
  return (
    <div className="absolute bottom-full left-0 mb-1 w-full max-w-xl max-h-64 overflow-y-auto rounded-md border border-gray-700 bg-gray-800 shadow-lg z-50">
      {matches.map((t, i) => (
        <button
          key={t.id}
          type="button"
          // mousedown (not click) + preventDefault so selecting doesn't blur the
          // textarea before the handler runs.
          onMouseDown={(e) => {
            e.preventDefault();
            onSelect(t);
          }}
          onMouseEnter={() => onHover(i)}
          className={cn(
            'w-full text-left px-3 py-2 flex flex-col',
            i === activeIndex ? 'bg-gray-700' : 'hover:bg-gray-700/50',
          )}
        >
          <span className="text-sm">
            <span className="text-gray-100">/{t.slug}</span>
            {t.argument_hint && (
              <span className="text-gray-500"> {formatArgs(t.argument_hint)}</span>
            )}
          </span>
          {t.description && (
            <span className="text-xs text-gray-400 truncate">{t.description}</span>
          )}
        </button>
      ))}
    </div>
  );
}
