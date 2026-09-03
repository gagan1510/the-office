export function isActiveRun(status) {
  return ['starting', 'running', 'waiting_for_lock', 'awaiting_approval'].includes(status);
}

export function sessionUsageLabel(usage, inputPrice, outputPrice, warningThreshold) {
  const turns = Number(usage?.turns || 0);
  const input = Number(usage?.inputTokens || 0);
  const output = Number(usage?.outputTokens || 0);
  const total = Number(usage?.totalTokens || input + output);
  if (!turns && !total) return 'usage appears after the CLI reports token counts';
  const cost = (input * Number(inputPrice || 0) + output * Number(outputPrice || 0)) / 1_000_000;
  return `${turns} turns · ${total.toLocaleString()} observed session tokens${cost ? ` · ~${cost.toLocaleString(undefined, {style:'currency', currency:'USD', maximumFractionDigits:2})}` : ''}${total >= Number(warningThreshold || 150000) ? ' · consider starting a fresh session' : ''}`;
}

window.OfficeFloorGrid = {isActiveRun, sessionUsageLabel};
