import DOMPurify from 'isomorphic-dompurify';

/**
 * Whitelist of allowed HTML tags for rich text content
 */
const ALLOWED_TAGS = ['strong', 'em', 'u', 'a', 'span'];

/**
 * Whitelist of allowed HTML attributes
 */
const ALLOWED_ATTR = ['href', 'target', 'rel', 'data-review'];

/**
 * Sanitizes HTML content using DOMPurify with a strict whitelist.
 * Only allows bold, italic, underline, link and red-review formatting.
 * Note: `style` is deliberately NOT allowed - red review text is a
 * `<span data-review="red">` coloured by CSS, never an inline style.
 * Uses isomorphic-dompurify which works in both browser and Node.js.
 *
 * @param dirty - The unsanitized HTML string
 * @returns Sanitized HTML string safe for rendering
 */
export function sanitizeHtml(dirty: string): string {
  return DOMPurify.sanitize(dirty, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    FORCE_BODY: true,
  });
}
