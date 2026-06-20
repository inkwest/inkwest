import { Image, Mic, Video } from 'lucide-react';

// Badged input modalities (TEXT is universal → never badged). Single source of truth for
// the icon + ALL-CAPS tooltip, shared by the admin model list and the chat model picker
// (via the ModalityIcons component). SPEECH = audio input in Bedrock terms.
export type InputModality = 'IMAGE' | 'VIDEO' | 'SPEECH';
export const MODALITY_META: Record<InputModality, { Icon: typeof Image; tip: string }> = {
  IMAGE: { Icon: Image, tip: 'IMAGE — image input' },
  VIDEO: { Icon: Video, tip: 'VIDEO — video input' },
  SPEECH: { Icon: Mic, tip: 'SPEECH — audio input' },
};
export const INPUT_MODALITIES = Object.keys(MODALITY_META) as InputModality[];

export const hasModality = (list: string[] | undefined, name: string) =>
  !!list?.some((x) => x.toUpperCase() === name);
