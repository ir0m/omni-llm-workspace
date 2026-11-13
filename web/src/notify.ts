/** Browser notifications for approvals and finished replies, shown only while the tab is hidden.
 * They need HTTPS (Tailscale Serve) or localhost; elsewhere they quietly do nothing. */

const supported = () => typeof window !== 'undefined' && 'Notification' in window && window.isSecureContext

export function askToNotify() {
  if (supported() && Notification.permission === 'default') Notification.requestPermission().catch(() => undefined)
}

export function notify(title: string, body: string) {
  if (!supported() || Notification.permission !== 'granted' || !document.hidden) return
  try {
    new Notification(title, { body, tag: 'omni-llm-workspace' })
  } catch {
    /* some mobile browsers only allow notifications from a service worker */
  }
}
