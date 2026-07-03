import { ButtonHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

export interface ToggleSwitchProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange'> {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** sm fits dense list rows; md for standalone form fields. */
  size?: 'sm' | 'md';
}

// iOS-style switch replacing bare checkboxes: green track when on, gray when off,
// sliding knob. Rendered as a real button with role="switch" so keyboard +
// screen-reader semantics come for free (and jest-dom's toBeChecked() works).
export function ToggleSwitch({
  checked,
  onChange,
  size = 'sm',
  className,
  onClick,
  disabled,
  ...rest
}: ToggleSwitchProps) {
  const dims =
    size === 'sm'
      ? { track: 'h-4 w-7', knob: 'h-3 w-3', on: 'translate-x-3.5' }
      : // 36px track − 16px knob − 2px inset = 18px travel (4.5 isn't in the default scale)
        { track: 'h-5 w-9', knob: 'h-4 w-4', on: 'translate-x-[18px]' };
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={(e) => {
        // Let callers intercept (e.g. stopPropagation inside a clickable row).
        onClick?.(e);
        onChange(!checked);
      }}
      className={cn(
        'relative inline-flex shrink-0 items-center rounded-full transition-colors duration-200',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 focus-visible:ring-offset-gray-900',
        dims.track,
        checked ? 'bg-green-600' : 'bg-gray-600',
        disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer',
        className,
      )}
      {...rest}
    >
      <span
        className={cn(
          'inline-block transform rounded-full bg-white shadow transition-transform duration-200',
          dims.knob,
          checked ? dims.on : 'translate-x-0.5',
        )}
      />
    </button>
  );
}
