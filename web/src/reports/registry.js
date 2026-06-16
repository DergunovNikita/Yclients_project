export const GROUP_LABELS = {
  finance: 'Финансы',
  operations: 'Операционка',
  team: 'Команда',
  clients: 'Клиенты',
  services: 'Услуги',
  churn: 'Отток',
  goods: 'Товары',
  marketing: 'Маркетинг',
  plans: 'Планы',
  milena: 'Методология Милены',
  diagnostics: 'Диагностика',
  advanced: 'Расширенные',
};

export const STATUS_LABELS = {
  ready: 'Работает',
  partial: 'Частично',
  source_missing: 'Нужен источник',
  planned: 'Запланирован',
};

export const SOURCE_LABELS = {
  market_benchmark_data: 'рыночные бенчмарки',
  milena_methodology_settings: 'настройки методологии',
  scheduled_report_calculation: 'фоновый расчет',
  telegram_nps: 'Telegram NPS',
  yandex_metrika: 'Яндекс.Метрика',
  yclients: 'YClients',
  yclients_comments: 'отзывы YClients',
};

export function sourceLabel(source) {
  return SOURCE_LABELS[source] || source;
}
