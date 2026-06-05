import { escapeHtml, formatValue } from '../format.js';
import { sourceLabel } from '../registry.js';

function renderCards(cards = []) {
  if (!cards.length) return '';
  return `
    <div class="reports-metrics">
      ${cards.map((card) => `
        <article class="reports-metric">
          <div class="reports-metric__label">${escapeHtml(card.label)}</div>
          <div class="reports-metric__value">${escapeHtml(formatValue(card.value, card.format))}</div>
        </article>
      `).join('')}
    </div>
  `;
}

function renderNotes(notes = [], missingSources = []) {
  const sourceText = missingSources.length
    ? `<div class="reports-note__sources">Не хватает: ${missingSources.map(sourceLabel).map(escapeHtml).join(', ')}</div>`
    : '';
  return `
    ${notes.map((note) => `
      <div class="reports-note reports-note--${escapeHtml(note.kind || 'info')}">
        <strong>${escapeHtml(note.title || 'Примечание')}</strong>
        <span>${escapeHtml(note.text || '')}</span>
        ${sourceText}
      </div>
    `).join('')}
  `;
}

function renderCharts(charts = []) {
  if (!charts.length) return '';
  return `
    <div class="reports-chart-grid">
      ${charts.map((chart) => `
        <section class="reports-panel">
          <div class="reports-panel__head">
            <h3>${escapeHtml(chart.title || 'График')}</h3>
          </div>
          <div class="reports-chart-box">
            <canvas data-report-chart="${escapeHtml(chart.id)}"></canvas>
          </div>
        </section>
      `).join('')}
    </div>
  `;
}

function renderTables(tables = []) {
  return tables.map((table) => {
    const rows = table.rows || [];
    const columns = table.columns || [];
    return `
      <section class="reports-panel reports-panel--wide">
        <div class="reports-panel__head">
          <h3>${escapeHtml(table.title || 'Таблица')}</h3>
          <span>${rows.length.toLocaleString('ru-RU')} строк</span>
        </div>
        ${rows.length ? `
          <div class="reports-table-scroll">
            <table class="reports-table">
              <thead>
                <tr>
                  ${columns.map((column) => `<th class="${column.format !== 'text' && column.format !== 'date' ? 'number' : ''}">${escapeHtml(column.label)}</th>`).join('')}
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => `
                  <tr>
                    ${columns.map((column) => `
                      <td class="${column.format !== 'text' && column.format !== 'date' ? 'number' : ''}">
                        ${escapeHtml(formatValue(row[column.key], column.format))}
                      </td>
                    `).join('')}
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        ` : '<div class="empty compact">Нет строк за выбранный период</div>'}
      </section>
    `;
  }).join('');
}

function renderUnavailable(data) {
  const label = data.source_status === 'planned' ? 'Отчет запланирован' : 'Источник данных не подключен';
  return `
    <div class="reports-unavailable">
      <h3>${escapeHtml(label)}</h3>
      ${renderNotes(data.notes || [], data.missing_sources || [])}
    </div>
  `;
}

function renderComparison(data) {
  const comparison = data.comparison;
  if (!comparison?.cards?.length) return '';
  return `
    <section class="reports-panel reports-panel--wide reports-compare">
      <div class="reports-panel__head">
        <h3>Сравнение</h3>
        <span>${escapeHtml(comparison.period?.start || '')} .. ${escapeHtml(comparison.period?.end || '')}</span>
      </div>
      ${renderCards(comparison.cards)}
    </section>
  `;
}

export function renderReportData(container, data, chartManager) {
  chartManager.clear();
  if (!data) {
    container.innerHTML = '<div class="empty compact">Нет данных отчета</div>';
    return;
  }
  if (data.source_status === 'missing' || data.source_status === 'planned') {
    container.innerHTML = renderUnavailable(data);
    return;
  }

  container.innerHTML = `
    ${renderNotes(data.notes || [], data.missing_sources || [])}
    ${renderCards(data.cards || [])}
    ${renderComparison(data)}
    ${renderCharts(data.charts || [])}
    ${renderTables(data.tables || [])}
  `;
  (data.charts || []).forEach((chart) => {
    chartManager.render(container.querySelector(`[data-report-chart="${chart.id}"]`), chart);
  });
}
