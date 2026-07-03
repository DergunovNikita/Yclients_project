import { t } from '../i18n.js';

export const GROUP_LABELS = {
  finance: t('reports.groups.finance'),
  operations: t('reports.groups.operations'),
  team: t('reports.groups.team'),
  clients: t('reports.groups.clients'),
  services: t('reports.groups.services'),
  churn: t('reports.groups.churn'),
  goods: t('reports.groups.goods'),
  marketing: t('reports.groups.marketing'),
  plans: t('reports.groups.plans'),
  milena: t('reports.groups.milena'),
  diagnostics: t('reports.groups.diagnostics'),
  advanced: t('reports.groups.advanced'),
};

export const STATUS_LABELS = {
  ready: t('reports.status.ready'),
  partial: t('reports.status.partial'),
  source_missing: t('reports.status.sourceMissing'),
  planned: t('reports.status.planned'),
};

export const SOURCE_LABELS = {
  market_benchmark_data: t('reports.sources.marketBenchmarkData'),
  milena_methodology_settings: t('reports.sources.milenaMethodologySettings'),
  scheduled_report_calculation: t('reports.sources.scheduledReportCalculation'),
  telegram_nps: 'Telegram NPS',
  yandex_metrika: t('reports.sources.yandexMetrika'),
  yclients: 'YClients',
  yclients_comments: t('reports.sources.yclientsComments'),
};

export function sourceLabel(source) {
  return SOURCE_LABELS[source] || source;
}
