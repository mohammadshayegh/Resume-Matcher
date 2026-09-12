import { Mark, mergeAttributes } from '@tiptap/react';

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    redText: {
      /** Toggle red review colour on the current selection */
      toggleRedText: () => ReturnType;
    };
  }
}

/**
 * Red Text Mark
 *
 * Marks a run of text for review by colouring it Alert Red (#DC2626).
 *
 * ATS safety: the mark renders a plain `<span data-review="red">`. It adds no
 * text (no markers, no pseudo-content) and no inline styles, so the text layer
 * extracted from the PDF is byte-identical to the unmarked bullet. The colour
 * itself comes from a CSS rule in globals.css, which applies in the app, the
 * /print route and therefore the Chromium-rendered PDF.
 */
export const RedText = Mark.create({
  name: 'redText',

  parseHTML() {
    return [
      { tag: 'span[data-review="red"]' },
      // Tolerate red spans produced elsewhere (e.g. pasted content) so the
      // toolbar reports the right active state instead of silently dropping it.
      {
        tag: 'span[style]',
        getAttrs: (element: HTMLElement | string): false | null => {
          if (typeof element === 'string') return false;
          const color = element.style.color.replace(/\s/g, '').toLowerCase();
          return color === '#dc2626' || color === 'rgb(220,38,38)' ? null : false;
        },
      },
    ];
  },

  renderHTML({ HTMLAttributes }) {
    return ['span', mergeAttributes(HTMLAttributes, { 'data-review': 'red' }), 0];
  },

  addCommands() {
    return {
      toggleRedText:
        () =>
        ({ commands }) =>
          commands.toggleMark(this.name),
    };
  },

  addKeyboardShortcuts() {
    return {
      'Mod-Shift-r': () => this.editor.commands.toggleRedText(),
    };
  },
});

export default RedText;
