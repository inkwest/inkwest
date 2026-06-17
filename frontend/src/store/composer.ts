import { create } from 'zustand';

// One-shot channel to push text into the active composer (ChatInput) from
// elsewhere in the tree — e.g. "reuse this prompt" on a past user message.
// `nonce` is bumped on every inject so re-sending the *same* text still fires
// the consuming effect (a bare `pending` value wouldn't change between two
// identical injects).
//
// Assumes exactly one ChatInput is mounted when `inject` fires (the trigger —
// MessageBubble — only renders alongside the composer). The consumer clears
// `pending` immediately, so a later-mounted composer can't pick up stale text;
// if that single-consumer invariant ever breaks, revisit this design.
interface ComposerState {
  pending: string | null;
  nonce: number;
  inject: (text: string) => void;
  consume: () => void;
}

export const useComposerStore = create<ComposerState>((set) => ({
  pending: null,
  nonce: 0,
  inject: (text) => set((s) => ({ pending: text, nonce: s.nonce + 1 })),
  consume: () => set({ pending: null }),
}));
