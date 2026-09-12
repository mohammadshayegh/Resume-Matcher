import { describe, expect, it } from 'vitest';
import { Editor } from '@tiptap/core';
import Document from '@tiptap/extension-document';
import Paragraph from '@tiptap/extension-paragraph';
import Text from '@tiptap/extension-text';
import { RedText } from '@/components/ui/extensions/red-text';
import { sanitizeHtml } from '@/lib/utils/html-sanitizer';

/**
 * The "Red review text" mark must survive the round trip
 * editor -> stored HTML -> sanitizeHtml -> resume/PDF render,
 * and must never alter the text itself (ATS safety).
 */

function makeEditor(content: string): Editor {
  return new Editor({
    extensions: [Document, Paragraph, Text, RedText],
    content,
  });
}

describe('RedText mark', () => {
  it('wraps the selected text in a data-review span without changing the text', () => {
    const editor = makeEditor('<p>Built a platform</p>');
    // Select "Built" (doc position 1..6)
    editor.commands.setTextSelection({ from: 1, to: 6 });
    editor.commands.toggleRedText();

    const html = editor.getHTML();
    expect(html).toContain('<span data-review="red">Built</span>');
    expect(editor.getText()).toBe('Built a platform');
    editor.destroy();
  });

  it('toggles the mark off again', () => {
    const editor = makeEditor('<p><span data-review="red">Built</span> a platform</p>');
    editor.commands.setTextSelection({ from: 1, to: 6 });
    expect(editor.isActive('redText')).toBe(true);

    editor.commands.toggleRedText();
    expect(editor.getHTML()).not.toContain('data-review');
    expect(editor.getText()).toBe('Built a platform');
    editor.destroy();
  });

  it('adopts a red inline-styled span pasted from elsewhere', () => {
    const editor = makeEditor('<p><span style="color: #DC2626">Built</span> a platform</p>');
    editor.commands.setTextSelection({ from: 1, to: 6 });
    expect(editor.isActive('redText')).toBe(true);
    // Re-rendered as our attribute-based span, never as an inline style
    expect(editor.getHTML()).toContain('data-review="red"');
    expect(editor.getHTML()).not.toContain('style=');
    editor.destroy();
  });

  it('ignores a non-red styled span', () => {
    const editor = makeEditor('<p><span style="color: #00FF00">Built</span> a platform</p>');
    editor.commands.setTextSelection({ from: 1, to: 6 });
    expect(editor.isActive('redText')).toBe(false);
    editor.destroy();
  });
});

describe('RedText survives sanitization', () => {
  it('keeps the red review span', () => {
    const out = sanitizeHtml('Built a <span data-review="red">secure</span> platform');
    expect(out).toContain('<span data-review="red">secure</span>');
  });

  it('strips inline styles from spans (no style injection path)', () => {
    const out = sanitizeHtml('<span style="position:fixed;color:red">x</span>');
    expect(out).not.toContain('style');
    expect(out).toContain('x');
  });

  it('never changes the extracted text (ATS safety)', () => {
    const raw = 'Built a <span data-review="red">secure</span> platform';
    const stripped = sanitizeHtml(raw).replace(/<[^>]+>/g, '');
    expect(stripped).toBe('Built a secure platform');
  });
});
