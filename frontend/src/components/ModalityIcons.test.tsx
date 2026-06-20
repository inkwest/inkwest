import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ModalityIcons } from './ModalityIcons';

describe('ModalityIcons', () => {
  it('renders nothing for a text-only (or unknown) model', () => {
    const { container } = render(<ModalityIcons modalities={['TEXT']} />);
    expect(container).toBeEmptyDOMElement();
    const { container: c2 } = render(<ModalityIcons modalities={undefined} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('renders one tooltip-labelled icon per badged modality, TEXT excluded', () => {
    render(<ModalityIcons modalities={['TEXT', 'IMAGE', 'SPEECH']} />);
    expect(screen.getByLabelText('IMAGE — image input')).toBeInTheDocument();
    expect(screen.getByLabelText('SPEECH — audio input')).toBeInTheDocument();
    expect(screen.queryByLabelText('VIDEO — video input')).not.toBeInTheDocument();
  });

  it('matches modalities case-insensitively', () => {
    render(<ModalityIcons modalities={['video']} />);
    expect(screen.getByLabelText('VIDEO — video input')).toBeInTheDocument();
  });
});
