export async function responseError(response: Response): Promise<string> {
  const fallback = `${response.status} ${response.statusText}`.trim()
  if (!response.headers.get('content-type')?.includes('application/json')) return fallback

  const payload: unknown = await response.json()
  if (!payload || typeof payload !== 'object' || !('detail' in payload)) return fallback
  const detail = payload.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.flatMap((item) => (
      item && typeof item === 'object' && typeof item.msg === 'string' ? [item.msg] : []
    )).join(' · ') || fallback
  }
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') {
    return detail.message
  }
  return fallback
}
