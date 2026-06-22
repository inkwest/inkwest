import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ModalityIcons } from './ModalityIcons';

describe('ModalityIcons', () => {
  it('renders the TEXT icon for a text-declaring or unknown model', () => {
    render(<ModalityIcons modalities={['TEXT']} />);
    expect(screen.getByLabelText('TEXT — text input')).toBeInTheDocument();
    render(<ModalityIcons modalities={undefined} />);
    expect(screen.getAllByLabelText('TEXT — text input').length).toBeGreaterThan(0);
  });

  it('renders the TEXT icon plus one tooltip-labelled icon per badged modality', () => {
    render(<ModalityIcons modalities={['TEXT', 'IMAGE', 'SPEECH']} />);
    expect(screen.getByLabelText('TEXT — text input')).toBeInTheDocument();
    expect(screen.getByLabelText('IMAGE — image input')).toBeInTheDocument();
    expect(screen.getByLabelText('SPEECH — audio input')).toBeInTheDocument();
    expect(screen.queryByLabelText('VIDEO — video input')).not.toBeInTheDocument();
  });

  it('omits the TEXT icon for a model that declares only non-text input', () => {
    render(<ModalityIcons modalities={['IMAGE']} />);
    expect(screen.getByLabelText('IMAGE — image input')).toBeInTheDocument();
    expect(screen.queryByLabelText('TEXT — text input')).not.toBeInTheDocument();
    // And nothing at all when the only declared modality is unknown to us.
    const { container: c2 } = render(<ModalityIcons modalities={['EMBEDDING']} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('matches modalities case-insensitively', () => {
    render(<ModalityIcons modalities={['video']} />);
    expect(screen.getByLabelText('VIDEO — video input')).toBeInTheDocument();
  });
});
