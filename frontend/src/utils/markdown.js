// Minimal markdown rendering for `/chat` answer bubbles — backend answers
// use markdown tables/bold (see app/agent's answer-formatting prompt), so a
// plain-text render would show literal `**B00HEON30Y**` and `| asin | roi |`
// pipes instead of formatting. `marked` is the smallest well-maintained
// option that handles GFM tables out of the box.
//
// `marked` does not sanitize raw HTML embedded in its markdown input by
// default. The answer text is LLM-generated but grounded in DB content
// (ASIN titles, etc.) that this app doesn't otherwise treat as trusted, so
// raw HTML/inline `<script>` tags are escaped instead of passed through —
// cheaper than pulling in a full sanitizer (DOMPurify) for a single-use
// case, and markdown *formatting* (bold/tables/lists) still works fully.
import { marked } from 'marked'

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

const renderer = new marked.Renderer()
renderer.html = (token) => escapeHtml(typeof token === 'string' ? token : token.text)

marked.setOptions({ renderer, gfm: true, breaks: true })

export function renderMarkdown(text) {
  if (!text) return ''
  return marked.parse(text)
}
