/** Pure terminal-result classification. An accepted request is NOT a deletion success. */
export function deletionOutcome(
  state: string,
  result: { status?: unknown; failed?: unknown } = {},
  requireResult = false,
): 'running' | 'success' | 'partial' | 'failed' | 'needs_review' {
  if (!['success', 'failed', 'cancelled', 'stopped', 'done', 'error'].includes(state))
    return 'running';
  if (['failed', 'cancelled', 'stopped', 'error'].includes(state)) return 'failed';
  const status = String(result.status ?? '');
  if (['error', 'failed', 'busy'].includes(status)) return 'failed';
  if (requireResult && status !== 'success') return 'needs_review';
  if (Number(result.failed || 0) > 0) return 'partial';
  return 'success';
}
