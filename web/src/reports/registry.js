import { t } from '../i18n.js';

// Catalog order is the backend's (REPORT_GROUP_ORDER); these are only the names.
export const GROUP_LABELS = {
  finance: t('reports.groups.finance'),
  services: t('reports.groups.services'),
  team: t('reports.groups.team'),
  operations: t('reports.groups.operations'),
  clients: t('reports.groups.clients'),
  goods: t('reports.groups.goods'),
  diagnostics: t('reports.groups.diagnostics'),
};

const SOURCE_LABELS = {
  appointments_detail: t('reports.sources.appointmentsDetail'),
  financial_transactions_detail: t('reports.sources.financialTransactionsDetail'),
  goods_transactions_detail: t('reports.sources.goodsTransactionsDetail'),
  staff_schedules: t('reports.sources.staffSchedules'),
  personal_account_topups: t('reports.sources.personalAccountTopups'),
};

export function sourceLabel(source) {
  return SOURCE_LABELS[source] || source;
}
