// Summaries shaped like /dashboard/bundle `summary`, one per branch of the card builders.

export const FULL = {
  revenue: {
    total: 1234567.5,
    change_pct: 12.34,
    appointments: 842,
    appointments_change_pct: -3.2,
    service_revenue: 1000000,
    service_revenue_change_pct: 0,
    goods_revenue: 150000.4,
    goods_revenue_change_pct: null,
    extra_service_revenue: 84567.1,
    extra_service_revenue_change_pct: 0.0004,
    service_count: 1200,
    service_count_change_pct: 5,
    goods_count: 310,
    goods_count_change_pct: -0.55,
    extra_service_count: 99,
    extra_service_count_change_pct: 100,
  },
  average_check: {
    source_status: 'ready',
    total: 1466.2,
    total_change_pct: 1.5,
    services: 1100.9,
    services_change_pct: -2,
    goods: 480,
    goods_change_pct: null,
    extra_services: 850.5,
    extra_services_change_pct: 7.77,
  },
  visit_metrics: {
    opz_qty: 55,
    opz_qty_change_pct: 10,
    opz_pct: 6.53,
    opz_pct_change_pct: -1.1,
    first_visits: 125,
    repeat_visits: 717,
    opz_first_visit_qty: 6,
    opz_first_visit_pct: 4.8,
    opz_first_visit_pct_change_pct: -0.4,
    opz_repeat_visit_qty: 49,
    opz_repeat_visit_pct: 6.82,
    opz_repeat_visit_pct_change_pct: null,
    extra_services_per_appointment_pct: 11.76,
    extra_services_per_appointment_pct_change_pct: 2.5,
    extra_service_clients: 40,
    extra_service_clients_pct: 7.1,
    unique_clients: 600,
    unique_clients_change_pct: 4,
    visits_per_client: 1.4033,
    visits_per_client_change_pct: -0.3,
    new_clients: 120,
    new_clients_change_pct: 8,
    new_clients_pct: 20,
    new_clients_pct_change_pct: 1,
    repeat_clients: 480,
    repeat_clients_change_pct: 3,
    repeat_clients_pct: 80,
    repeat_clients_pct_change_pct: -1,
    client_visit_frequency: {
      one_visit: { count: 300, pct: 50 },
      two_to_three_visits: { count: 200, pct: 33.3 },
      four_plus_visits: { count: 100, pct: 16.7 },
    },
  },
  appointments_breakdown: {
    source_status: 'ready',
    total: 1000,
    total_share_pct: 100,
    cancelled: 50,
    cancelled_share_pct: 5,
    completed: 842,
    completed_share_pct: 84.2,
    incomplete: 108,
    incomplete_share_pct: 10.8,
  },
};

export const PARTIAL_AVERAGE_CHECK = {
  ...FULL,
  average_check: { ...FULL.average_check, source_status: 'partial' },
  appointments_breakdown: { ...FULL.appointments_breakdown, source_status: 'local', local_reason: 'upstream_error' },
};

// The administrator's own Plan/fact row for the window (`administrator_personal`), manual top-up of 3 included.
const ADMIN_BLOCK_METRICS = {
  revenue: { value: 31200.5, previous_value: 28000, change_pct: 11.43 },
  avg_check_total: { value: 1040.02, previous_value: 1000, change_pct: 4 },
  clients: { value: 33, previous_value: 61, change_pct: -45.9 },
  cosmo_qty: { value: 12, previous_value: 12, change_pct: 0 },
  cosmo_sum: { value: 9800, previous_value: 11000, change_pct: -10.91 },
  opz_qty: { value: 8, previous_value: 8, change_pct: 0 },
  opz_pct: { value: 24.24, previous_value: 13.11, change_pct: 84.9 },
  extra_services_qty: { value: 16, previous_value: 14, change_pct: 14.29 },
  extra_services_pct: { value: 37.21, previous_value: 22.58, change_pct: 64.79 },
  reviews_qty: { value: 2, previous_value: 6, change_pct: -66.67 },
};

export const ADMIN_SCHEDULE = {
  ...FULL,
  service_attribution: { mode: 'administrator_schedule', appointment_count: 321, unique_client_count: 250 },
  administrator_personal: { staff_id: 7, company_id: 1, metrics: ADMIN_BLOCK_METRICS, opz_manual_qty: 1 },
};

// Manager default: money hidden, so every revenue field is null and the block is flagged.
export const ADMIN_SCHEDULE_FINANCIALS_HIDDEN = {
  ...ADMIN_SCHEDULE,
  financials_hidden: true,
  revenue: {
    ...FULL.revenue,
    total: null,
    service_revenue: null,
    goods_revenue: null,
    extra_service_revenue: null,
  },
  average_check: { ...FULL.average_check, total: null, services: null, goods: null, extra_services: null },
  administrator_personal: {
    ...ADMIN_SCHEDULE.administrator_personal,
    metrics: Object.fromEntries(
      Object.entries(ADMIN_BLOCK_METRICS).filter(([code]) => !['revenue', 'avg_check_total', 'cosmo_sum'].includes(code)),
    ),
  },
  appointments_breakdown: { source_status: 'unavailable' },
};

export const NO_BLOCKS = { appointments_breakdown: { source_status: 'local', local_reason: 'upstream_error' } };

export const SUMMARIES = {
  full: FULL,
  partialAverageCheck: PARTIAL_AVERAGE_CHECK,
  adminSchedule: ADMIN_SCHEDULE,
  adminScheduleFinancialsHidden: ADMIN_SCHEDULE_FINANCIALS_HIDDEN,
  noBlocks: NO_BLOCKS,
};
