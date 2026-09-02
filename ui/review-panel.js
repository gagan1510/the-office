export function formatTimeline(repositories) {
  return (repositories || []).map(item => `# ${item.path}\n${item.patch || '[no changes captured for this run]'}`).join('\n\n');
}

window.OfficeReviewPanel = {formatTimeline};
