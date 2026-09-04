"""
Модуль для работы с YClients API
"""
import requests
import time
from datetime import date, timedelta
from typing import Dict, Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class YClientsAPI:
    """Класс для работы с YClients API"""

    MAX_PER_PAGE = 200
    # Budget for one dated window, scaled to the rows it actually yields — the work is
    # proportional to rows, not to days, so a per-day budget would collapse to a
    # constant margin on a busy branch. Measured cost is ~3 requests per page of
    # distinct rows (255 requests for a 26-year history of 16 463 rows; 31 for a
    # 90-day window of 2 130), so 12 leaves roughly a 4x margin. Only distinct rows
    # raise the ceiling: an endpoint that stops filtering keeps returning the same ids,
    # so its requests climb while its allowance does not.
    SPLIT_REQUESTS_BASE = 500
    SPLIT_REQUESTS_PER_PAGE = 12

    def __init__(
        self,
        partner_token: str,
        login: str,
        password: str,
        request_delay: float = 0.25,
        timeout: float = 30.0,
        retry_total: int = 3,
        retry_backoff: float = 1.0,
        retry_after_max: float = 60.0,
    ):
        self.partner_token = partner_token
        self.login = login
        self.password = password
        self.user_token: Optional[str] = None
        self._split_seen: Optional[set] = None
        self._split_requests = 0
        self.last_dated_fetch_complete = True
        self.base_url = 'https://api.yclients.com/api/v1'
        self.request_delay = max(0.0, request_delay)
        self.timeout = max(1.0, timeout)
        self.retry_after_max = max(0, int(retry_after_max))
        self.session = requests.Session()
        # retry_after_max caps a 429 Retry-After we will honor. urllib3's default is 21600s (6h),
        # so a large/hostile header could otherwise stall a sync step for hours.
        retry = Retry(
            total=max(0, retry_total),
            connect=max(0, retry_total),
            read=max(0, retry_total),
            status=max(0, retry_total),
            backoff_factor=max(0.0, retry_backoff),
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            retry_after_max=self.retry_after_max,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

    _UA = (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    )

    def authenticate(self) -> bool:
        url = f'{self.base_url}/auth'

        headers = {
            'Authorization': f'Bearer {self.partner_token}',
            'Accept': 'application/vnd.yclients.v2+json',
            'Content-Type': 'application/json',
            'User-Agent': self._UA,
        }

        payload = {
            'login': self.login,
            'password': self.password
        }

        try:
            response = self.session.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            print(f"✗ Ошибка авторизации: {e}")
            return False

        if response.status_code in [200, 201]:
            data = response.json()
            if data.get('success'):
                self.user_token = data.get('data', {}).get('user_token')
                return True
            else:
                print(f"✗ Авторизация не удалась: {data}")
                return False
        else:
            print(f"✗ Ошибка авторизации: {response.status_code}")
            print(f"Ответ: {response.text}")
            return False

    def _get_headers(self) -> Dict[str, str]:
        if not self.user_token:
            raise ValueError("Необходимо сначала выполнить авторизацию")

        return {
            'Authorization': f'Bearer {self.partner_token}, User {self.user_token}',
            'Accept': 'application/vnd.yclients.v2+json',
            'Content-Type': 'application/json',
            'User-Agent': self._UA,
        }

    def _ensure_auth(self) -> bool:
        if not self.user_token:
            return self.authenticate()
        return True

    def _get(self, url: str, params: dict = None) -> Optional[dict]:
        """Единый GET-запрос с throttling."""
        headers = self._get_headers()
        time.sleep(self.request_delay)
        try:
            response = self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            print(f"✗ GET {url} — ошибка сети: {e}")
            return None

        if response.status_code == 200:
            return response.json()
        else:
            print(f"✗ GET {url} — {response.status_code}")
            print(f"Ответ: {response.text[:300]}")
            return None

    def _get_all_pages(self, url: str, extra_params: dict = None,
                       page_limit: int = None) -> List[Dict]:
        """Постраничная загрузка всех записей из endpoint с пагинацией.

        `page_limit` stops after that many pages; a caller that only needs to know
        whether the response spills past one page must not pay for the rest of it.
        """
        all_items = []
        page = 1

        while True:
            self._spend_split_budget(url)
            params = {'page': page, 'count': self.MAX_PER_PAGE}
            if extra_params:
                params.update(extra_params)

            result = self._get(url, params)
            if result is None:
                raise RuntimeError(f"Failed to fetch paginated YClients endpoint {url} page {page}")

            if result.get('success') is False:
                raise RuntimeError(f"YClients endpoint {url} page {page} returned success=false")
            if 'data' not in result:
                raise RuntimeError(f"YClients endpoint {url} page {page} returned no data field")
            data = result.get('data') or []
            if not isinstance(data, list):
                raise RuntimeError(f"YClients endpoint {url} page {page} returned non-list data")
            if not data:
                break

            all_items.extend(data)

            meta = result.get('meta') or {}
            total_count = (meta.get('total_count') if isinstance(meta, dict) else None) or result.get('count')
            if total_count and len(all_items) >= total_count:
                break

            if len(data) < self.MAX_PER_PAGE:
                break

            if page_limit is not None and page >= page_limit:
                break

            page += 1

        return all_items

    def _spend_split_budget(self, url: str) -> None:
        """Stop a dated fetch whose requests grow without bringing back new rows.

        The split assumes the endpoint honours `start_date`/`end_date`. If one stops
        filtering — or starts filtering on another field — every probe comes back full,
        the range bisects down to single days, and each day then walks the entire
        dataset: a 90-day window over 100k rows costs one request when the filter works
        and tens of thousands when it does not. The allowance grows only with distinct
        rows, so the runaway stops once it starts re-serving ids it has already given —
        on a first pass over a large dataset that still costs thousands of requests, but
        it is bounded, where the unguarded loop was not. Honest work never approaches it:
        the split stops at leaves under one page, so it spends ~2 requests per 200 rows
        against an allowance of 12. Densely clustered data narrows that margin — a window
        whose rows sit in a few dense pockets can reach ~90% of its allowance — so a
        branch far outside the measured shapes is worth checking before assuming the
        message below names the real cause.

        Exhaustion is turned into a failed step by `run_sync_step` rather than unwinding,
        so the remaining branches and tenants still sync. Every endpoint that splits is a
        checkpoint step, so the run itself is still reported failed — and in a
        purge-and-reload mode the window's coverage has already been narrowed, so the next
        run re-fetches it.
        """
        if self._split_seen is None:
            return
        self._split_requests += 1
        allowed = (
            self.SPLIT_REQUESTS_BASE
            + self.SPLIT_REQUESTS_PER_PAGE * (len(self._split_seen) // self.MAX_PER_PAGE)
        )
        if self._split_requests > allowed:
            raise RuntimeError(
                f"YClients endpoint {url} spent {self._split_requests} requests on one dated "
                f"window for {len(self._split_seen)} distinct rows; the date filter is likely "
                "no longer honoured"
            )

    def _keep_new_rows(self, items: List[Dict]) -> List[Dict]:
        """Drop rows already returned by another piece of the same window.

        Recorded only for rows that are actually returned: a probe's page is thrown away
        when the range splits, and marking it seen would delete it from the children.
        """
        if self._split_seen is None:
            return items
        fresh = []
        for item in items:
            item_id = item.get('id') if isinstance(item, dict) else None
            if item_id is not None:
                if item_id in self._split_seen:
                    continue
                self._split_seen.add(item_id)
            fresh.append(item)
        return fresh

    def _fetch_single_page_ranges(self, url: str, start: date, end: date) -> List[Dict]:
        """Fetch [start, end], halving it until each request fits on one page.

        Many transactions share the exact same `date`, and the endpoint paginates by
        offset over a sort that does not break those ties deterministically. Rows on a
        page boundary therefore shift between requests: some arrive twice and others
        never arrive at all. Measured on one branch, June returned 639 rows over four
        pages and silently dropped a real 3 100 ₽ payment that a narrower request
        returned every time. A response that fits on a single page has no boundary to
        lose rows across, so the range is split until that holds.

        Only the first page is read before deciding, so a quiet range — an empty decade
        before the branch opened, most of all — costs one request instead of a walk
        through every month in it, and a dense range is split without first downloading
        rows that would be thrown away.
        """
        window = {'start_date': start.isoformat(), 'end_date': end.isoformat()}
        first_page = self._get_all_pages(url, window, page_limit=1)
        if len(first_page) < self.MAX_PER_PAGE:
            return self._keep_new_rows(first_page)
        if start >= end:
            # A single day busier than one page is the one case the split cannot fix:
            # the range filter has no finer grain, so its pages are read as they come
            # and this day alone stays exposed to the boundary shifting described above.
            whole_day = self._get_all_pages(url, window)
            if len(whole_day) > self.MAX_PER_PAGE:
                # A day that filled the probe exactly and held nothing more was never at
                # risk: one page has no boundary to lose rows across.
                self.last_dated_fetch_complete = False
                print(
                    f"  ! {start.isoformat()} не умещается на одну страницу; "
                    "строки этого дня читаются постранично и могут теряться"
                )
            return self._keep_new_rows(whole_day)

        mid = start + timedelta(days=(end - start).days // 2)
        return (
            self._fetch_single_page_ranges(url, start, mid)
            + self._fetch_single_page_ranges(url, mid + timedelta(days=1), end)
        )

    def _get_all_pages_dated(self, url: str, start_date: Optional[str],
                             end_date: Optional[str]) -> List[Dict]:
        """Fetch a dated endpoint in pieces small enough to page safely, de-duplicated.

        `last_dated_fetch_complete` reports whether every piece fit on one page. A caller
        that deletes its window before reloading must consult it: reloading a window that
        was read through shifting page boundaries would delete rows the fetch never
        returned, which is worse than keeping rows YClients has since removed.
        """
        if not start_date or not end_date:
            params = {k: v for k, v in (('start_date', start_date), ('end_date', end_date)) if v}
            return self._get_all_pages(url, params or None)

        self._split_seen = set()
        self._split_requests = 0
        self.last_dated_fetch_complete = True
        try:
            return self._fetch_single_page_ranges(
                url, date.fromisoformat(start_date), date.fromisoformat(end_date)
            )
        finally:
            self._split_seen = None
            self._split_requests = 0

    @staticmethod
    def _list_response_data(result: Optional[dict], endpoint: str) -> Optional[List[Dict]]:
        """Validate a list response before callers replace stored snapshots."""
        if result is None:
            return None
        if result.get('success') is not True:
            print(f"✗ YClients endpoint {endpoint} returned success!=true")
            return None
        if 'data' not in result:
            print(f"✗ YClients endpoint {endpoint} returned no data field")
            return None
        data = result.get('data')
        if not isinstance(data, list):
            print(f"✗ YClients endpoint {endpoint} returned non-list data")
            return None
        return data

    # ------------------------------------------------------------------
    # Сети и компании
    # ------------------------------------------------------------------

    def get_groups(self) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/groups')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Категории услуг
    # ------------------------------------------------------------------

    def get_service_categories(self, company_id: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/company/{company_id}/service_categories')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Услуги
    # ------------------------------------------------------------------

    def get_services(self, company_id: str, staff_id: Optional[int] = None,
                     category_id: Optional[int] = None) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None

        url = f'{self.base_url}/company/{company_id}/services'
        params = {}
        if staff_id:
            params['staff_id'] = staff_id
        if category_id:
            params['category_id'] = category_id

        result = self._get(url, params or None)
        return self._list_response_data(result, url)

    # ------------------------------------------------------------------
    # Должности
    # ------------------------------------------------------------------

    def get_positions(self, company_id: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/company/{company_id}/staff/positions/')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Сотрудники
    # ------------------------------------------------------------------

    def get_staff(self, company_id: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/company/{company_id}/staff')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Клиенты (с пагинацией)
    # ------------------------------------------------------------------

    def get_clients(self, company_id: str) -> List[Dict]:
        if not self._ensure_auth():
            return []
        url = f'{self.base_url}/clients/{company_id}'
        return self._get_all_pages(url)

    # ------------------------------------------------------------------
    # Кассы
    # ------------------------------------------------------------------

    def get_accounts(self, company_id: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/accounts/{company_id}')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Склады
    # ------------------------------------------------------------------

    def get_storages(self, company_id: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/storages/{company_id}')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Категории товаров
    # ------------------------------------------------------------------

    def get_good_categories(self, company_id: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(f'{self.base_url}/company/{company_id}/goods_categories/0')
        return result.get('data', []) if result else None

    # ------------------------------------------------------------------
    # Товары (с пагинацией)
    # ------------------------------------------------------------------

    def get_goods(self, company_id: str) -> List[Dict]:
        if not self._ensure_auth():
            return []
        url = f'{self.base_url}/goods/{company_id}'
        return self._get_all_pages(url)

    # ------------------------------------------------------------------
    # Записи / визиты (с пагинацией и фильтрами по датам)
    # ------------------------------------------------------------------

    def get_records(self, company_id: str,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None

        url = f'{self.base_url}/records/{company_id}'
        return self._get_all_pages_dated(url, start_date, end_date)

    # ------------------------------------------------------------------
    # Финансовые транзакции (с пагинацией и датами)
    # ------------------------------------------------------------------

    def get_financial_transactions(self, company_id: str,
                                   start_date: Optional[str] = None,
                                   end_date: Optional[str] = None) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None

        url = f'{self.base_url}/transactions/{company_id}'
        return self._get_all_pages_dated(url, start_date, end_date)

    # ------------------------------------------------------------------
    # Товарные транзакции (с пагинацией и датами)
    # ------------------------------------------------------------------

    def get_goods_transactions(self, company_id: str,
                               start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None

        url = f'{self.base_url}/storages/transactions/{company_id}'
        return self._get_all_pages_dated(url, start_date, end_date)

    # ------------------------------------------------------------------
    # Комментарии / отзывы (с пагинацией и датами)
    # ------------------------------------------------------------------

    def get_comments(self, company_id: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None

        url = f'{self.base_url}/comments/{company_id}/'
        return self._get_all_pages_dated(url, start_date, end_date)

    # ------------------------------------------------------------------
    # График работы сотрудников
    # ------------------------------------------------------------------

    def get_staff_schedule(self, company_id: str,
                           start_date: str, end_date: str,
                           staff_ids: Optional[List[int]] = None) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None

        params = {'start_date': start_date, 'end_date': end_date}
        if staff_ids:
            params['staff_ids[]'] = staff_ids

        result = self._get(f'{self.base_url}/company/{company_id}/staff/schedule',
                           params=params)
        return self._list_response_data(result, f'company/{company_id}/staff/schedule')

    # ------------------------------------------------------------------
    # Аналитика: основные показатели
    # ------------------------------------------------------------------

    def get_analytics_overall(self, company_id: str,
                              date_from: str, date_to: str) -> Optional[Dict]:
        if not self._ensure_auth():
            return None
        result = self._get(
            f'{self.base_url}/company/{company_id}/analytics/overall/',
            params={'date_from': date_from, 'date_to': date_to},
        )
        return result.get('data') if result else None

    # ------------------------------------------------------------------
    # Аналитика: дневные графики (income / records / fullness)
    # ------------------------------------------------------------------

    def _get_analytics_chart(self, company_id: str, chart: str,
                             date_from: str, date_to: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(
            f'{self.base_url}/company/{company_id}/analytics/overall/charts/{chart}/',
            params={'date_from': date_from, 'date_to': date_to},
        )
        if result is None:
            return None
        if isinstance(result, list):
            return result
        return result.get('data', []) if isinstance(result, dict) else None

    def get_analytics_income_daily(self, company_id: str,
                                   date_from: str, date_to: str) -> Optional[List[Dict]]:
        return self._get_analytics_chart(company_id, 'income_daily', date_from, date_to)

    def get_analytics_records_daily(self, company_id: str,
                                    date_from: str, date_to: str) -> Optional[List[Dict]]:
        return self._get_analytics_chart(company_id, 'records_daily', date_from, date_to)

    def get_analytics_fullness_daily(self, company_id: str,
                                     date_from: str, date_to: str) -> Optional[List[Dict]]:
        return self._get_analytics_chart(company_id, 'fullness_daily', date_from, date_to)

    # ------------------------------------------------------------------
    # Аналитика: источники и статусы записей
    # ------------------------------------------------------------------

    def get_analytics_record_source(self, company_id: str,
                                    date_from: str, date_to: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(
            f'{self.base_url}/company/{company_id}/analytics/overall/charts/record_source/',
            params={'date_from': date_from, 'date_to': date_to},
        )
        if result is None:
            return None
        if isinstance(result, list):
            return result
        return result.get('data', []) if isinstance(result, dict) else None

    def get_analytics_record_status(self, company_id: str,
                                    date_from: str, date_to: str) -> Optional[List[Dict]]:
        if not self._ensure_auth():
            return None
        result = self._get(
            f'{self.base_url}/company/{company_id}/analytics/overall/charts/record_status/',
            params={'date_from': date_from, 'date_to': date_to},
        )
        if result is None:
            return None
        if isinstance(result, list):
            return result
        return result.get('data', []) if isinstance(result, dict) else None

    # ------------------------------------------------------------------
    # Z-Отчёт
    # ------------------------------------------------------------------

    def get_z_report(self, company_id: str,
                     start_date: str) -> Optional[Dict]:
        if not self._ensure_auth():
            return None
        result = self._get(
            f'{self.base_url}/reports/z_report/{company_id}',
            params={'start_date': start_date},
        )
        return result.get('data') if result else None
