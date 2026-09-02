function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[character]));
}

export function pluginSuggestionMarkup(suggestions) {
  return (suggestions || []).map(item => `<div><b>${escapeHtml(item.name)}</b> · ${escapeHtml((item.matchedSignals || []).join(', '))}<br><small>${escapeHtml(item.source || 'Office heuristic')}</small> · ${escapeHtml(item.access)} <button type="button" data-plugin-approval="${escapeHtml(item.id)}">Approve for this floor</button></div>`).join('<br>');
}

window.OfficeSettings = {pluginSuggestionMarkup};
