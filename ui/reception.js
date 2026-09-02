export function fuzzyScore(text, query) {
  text = String(text).toLowerCase();
  query = String(query).toLowerCase().trim();
  if (!query) return 1;
  const direct = text.indexOf(query);
  if (direct >= 0) return 1000 - direct;
  let cursor = 0, score = 0;
  for (const char of query) {
    const found = text.indexOf(char, cursor);
    if (found < 0) return -1;
    score += 20 - (found - cursor);
    cursor = found + 1;
  }
  return score;
}

window.OfficeReception = {fuzzyScore};
