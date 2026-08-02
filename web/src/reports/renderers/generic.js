import { escapeHtml, formatValue } from '../format.js';
import { intlLocale, t } from '../../i18n.js';
import { sourceLabel } from '../registry.js';
import { rankingRowsForMetric, tableHasRows } from '../ranking.js';

export { rankingRowsForMetric } from '../ranking.js';

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
    ? `<div class="reports-note__sources">${t('reports.missingSources')}: ${missingSources.map(sourceLabel).map(escapeHtml).join(', ')}</div>`
    : '';
  return `
    ${notes.map((note) => `
      <div class="reports-note reports-note--${escapeHtml(note.kind || 'info')}">
        <strong>${escapeHtml(note.title || t('reports.note'))}</strong>
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
            <h3>${escapeHtml(chart.title || t('reports.chart'))}</h3>
          </div>
          <div class="reports-chart-box">
            <canvas data-report-chart="${escapeHtml(chart.id)}"></canvas>
          </div>
        </section>
      `).join('')}
    </div>
  `;
}

function rowsCountText(count) {
  return t('reports.rowsCount', { count: count.toLocaleString(intlLocale()) });
}

function renderTableRows(rows, columns) {
  return rows.map((row) => `
    <tr>
      ${columns.map((column) => `
        <td class="${column.format !== 'text' && column.format !== 'date' ? 'number' : ''}">
          ${escapeHtml(formatValue(row[column.key], column.format))}
        </td>
      `).join('')}
    </tr>
  `).join('');
}

function renderTables(tables = []) {
  return tables.map((table) => {
    if (table.hide_when_empty && !tableHasRows(table)) return '';
    const rows = table.rows || [];
    const columns = table.columns || [];
    const ranking = table.ranking;
    const rankingControl = ranking ? `
      <label class="reports-ranking-control">
        <span>${t('reports.rankingBy')}</span>
        <select data-ranking-table="${escapeHtml(table.id)}">
          ${(ranking.options || []).map((option) => `
            <option value="${escapeHtml(option.key)}"${option.key === ranking.default_metric ? ' selected' : ''}>
              ${escapeHtml(option.label)}
            </option>
          `).join('')}
        </select>
      </label>
    ` : '';
    return `
      <section class="reports-panel reports-panel--wide" data-report-table="${escapeHtml(table.id)}">
        <div class="reports-panel__head">
          <h3>${escapeHtml(table.title || t('reports.table'))}</h3>
          <div class="reports-panel__actions">
            ${rankingControl}
            <span data-ranking-count>${rowsCountText(rows.length)}</span>
          </div>
        </div>
        ${(rows.length || ranking) ? `
          <div class="reports-table-scroll"${rows.length ? '' : ' hidden'}>
            <table class="reports-table">
              <thead>
                <tr>
                  ${columns.map((column) => `<th class="${column.format !== 'text' && column.format !== 'date' ? 'number' : ''}">${escapeHtml(column.label)}</th>`).join('')}
                </tr>
              </thead>
              <tbody>
                ${renderTableRows(rows, columns)}
              </tbody>
            </table>
          </div>
          ${ranking ? `<div class="empty compact" data-ranking-empty${rows.length ? ' hidden' : ''}>${t('reports.noRowsForPeriod')}</div>` : ''}
        ` : `<div class="empty compact">${t('reports.noRowsForPeriod')}</div>`}
      </section>
    `;
  }).join('');
}

function wireRankingTables(container, tables = []) {
  const byId = new Map(tables.map((table) => [String(table.id), table]));
  container.querySelectorAll('[data-ranking-table]').forEach((select) => {
    select.addEventListener('change', () => {
      const table = byId.get(select.dataset.rankingTable);
      const panel = [...container.querySelectorAll('[data-report-table]')]
        .find((item) => item.dataset.reportTable === select.dataset.rankingTable);
      if (!table || !panel) return;
      const rows = rankingRowsForMetric(table, select.value);
      const scroll = panel.querySelector('.reports-table-scroll');
      const body = panel.querySelector('tbody');
      const empty = panel.querySelector('[data-ranking-empty]');
      const count = panel.querySelector('[data-ranking-count]');
      if (body) body.innerHTML = renderTableRows(rows, table.columns || []);
      if (scroll) scroll.hidden = rows.length === 0;
      if (empty) empty.hidden = rows.length > 0;
      if (count) count.textContent = rowsCountText(rows.length);
    });
  });
}

function renderUnavailable(data) {
  const label = data.source_status === 'planned' ? t('reports.plannedReport') : t('reports.sourceNotConnected');
  return `
    <div class="reports-unavailable">
      <h3>${escapeHtml(label)}</h3>
      ${renderNotes(data.notes || [], data.missing_sources || [])}
    </div>
  `;
}

function renderComparison(data) {
  const comparison = data.comparison;
  const rows = comparison?.rows || [];
  if (!rows.length && !comparison?.cards?.length) return '';
  const currentPeriod = data.period ? `${data.period.start || ''} .. ${data.period.end || ''}` : '';
  const comparePeriod = comparison.period ? `${comparison.period.start || ''} .. ${comparison.period.end || ''}` : '';
  const bodyRows = rows.length
    ? rows
    : (comparison.cards || []).map((card) => ({
        label: card.label,
        format: card.format,
        current: null,
        compare: card.value,
        delta: null,
        delta_pct: null,
      }));
  return `
    <section class="reports-panel reports-panel--wide reports-compare">
      <div class="reports-panel__head">
        <h3>${t('reports.comparison')}</h3>
        <span>${escapeHtml(currentPeriod)} / ${escapeHtml(comparePeriod)}</span>
      </div>
      <div class="reports-table-scroll">
        <table class="reports-table">
          <thead>
            <tr>
              <th>${t('reports.metric')}</th>
              <th class="number">${t('reports.currentPeriod')}</th>
              <th class="number">${t('reports.comparePeriod')}</th>
              <th class="number">Δ</th>
              <th class="number">Δ%</th>
            </tr>
          </thead>
          <tbody>
            ${bodyRows.map((row) => `
              <tr>
                <td>${escapeHtml(row.label || '')}</td>
                <td class="number">${escapeHtml(formatValue(row.current, row.format))}</td>
                <td class="number">${escapeHtml(formatValue(row.compare, row.format))}</td>
                <td class="number">${escapeHtml(formatValue(row.delta, row.format))}</td>
                <td class="number">${escapeHtml(formatValue(row.delta_pct, 'percent'))}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

export function renderReportData(container, data, chartManager) {
  chartManager.clear();
  if (!data) {
    container.innerHTML = `<div class="empty compact">${t('reports.noReportData')}</div>`;
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
  wireRankingTables(container, data.tables || []);
  (data.charts || []).forEach((chart) => {
    chartManager.render(container.querySelector(`[data-report-chart="${chart.id}"]`), chart);
  });
}
