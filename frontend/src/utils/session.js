// Shared by router/index.js (the bare `/chat` -> `/chat/:sessionId` redirect)
// and Chat.vue ("New chat") -- one place generating chat session ids so the
// two don't drift into slightly different formats.
export function newChatSessionId() {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `sess-${Date.now()}-${Math.random().toString(16).slice(2)}`
}
