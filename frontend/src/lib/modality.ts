import { Image, Mic, Type, Video } from 'lucide-react';

// Input modalities beyond text. Single source of truth for the icon + ALL-CAPS tooltip,
// shared by the admin model list and the chat model picker (via the ModalityIcons
// component). SPEECH = audio input in Bedrock terms.
export type InputModality = 'IMAGE' | 'VIDEO' | 'SPEECH';
export const MODALITY_META: Record<InputModality, { Icon: typeof Image; tip: string }> = {
  IMAGE: { Icon: Image, tip: 'IMAGE — image input' },
  VIDEO: { Icon: Video, tip: 'VIDEO — video input' },
  SPEECH: { Icon: Mic, tip: 'SPEECH — audio input' },
};
export const INPUT_MODALITIES = Object.keys(MODALITY_META) as InputModality[];

// TEXT isn't part of MODALITY_META (it's the baseline, not a badged extra) but it gets its
// own (T)ext icon — rendered by ModalityIcons when the model accepts text and used as the
// admin filter facet. Showing it alongside the others stops a lone IMAGE icon being misread
// as "image-only" rather than "text + image".
export const TEXT_META = { Icon: Type, tip: 'TEXT — text input' } as const;

export const hasModality = (list: string[] | undefined, name: string) =>
  !!list?.some((x) => x.toUpperCase() === name);
