export function csvCell(value) {
  let text = String(value ?? '');
  if (/^[=+\-@\t\r\n]/.test(text)) {
    text = `'${text}`;
  }
  return `"${text.replace(/"/g, '""')}"`;
}

export function buildCsv(rows, columns) {
  const header = columns.map((column) => csvCell(column.label)).join(',');
  const body = rows.map((row) => columns.map((column) => csvCell(row[column.key])).join(','));
  return [header, ...body].join('\r\n');
}
