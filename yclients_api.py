"""
Модуль для работы с YClients API
"""
import requests
import time
from typing import Dict, Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class YClientsAPI:
    """Класс для работы с YClients API"""

    MAX_PER_PAGE = 200

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

    def _get_all_pages(self, url: str, extra_params: dict = None) -> List[Dict]:
        """Постраничная загрузка всех записей из endpoint с пагинацией."""
        all_items = []
        page = 1

        while True:
            params = {'page': page, 'count': self.MAX_PER_PAGE}
            if extra_params:
                params.update(extra_params)

            result = self._get(url, params)
            if result is None:
                raise RuntimeError(f"Failed to fetch paginated YClients endpoint {url} page {page}")

            data = result.get('data', [])
            if not data:
                break

            all_items.extend(data)

            meta = result.get('meta') or {}
            total_count = (meta.get('total_count') if isinstance(meta, dict) else None) or result.get('count')
            if total_count and len(all_items) >= total_count:
                break

            if len(data) < self.MAX_PER_PAGE:
                break

            page += 1

        return all_items

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
        return result.get('data', []) if result else None

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
                    end_date: Optional[str] = None) -> List[Dict]:
        if not self._ensure_auth():
            return []

        url = f'{self.base_url}/records/{company_id}'
        params = {}
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date

        return self._get_all_pages(url, params or None)

    # ------------------------------------------------------------------
    # Финансовые транзакции (с пагинацией и датами)
    # ------------------------------------------------------------------

    def get_financial_transactions(self, company_id: str,
                                   start_date: Optional[str] = None,
                                   end_date: Optional[str] = None) -> List[Dict]:
        if not self._ensure_auth():
            return []

        url = f'{self.base_url}/transactions/{company_id}'
        params = {}
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date

        return self._get_all_pages(url, params or None)

    # ------------------------------------------------------------------
    # Товарные транзакции (с пагинацией и датами)
    # ------------------------------------------------------------------

    def get_goods_transactions(self, company_id: str,
                               start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> List[Dict]:
        if not self._ensure_auth():
            return []

        url = f'{self.base_url}/storages/transactions/{company_id}'
        params = {}
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date

        return self._get_all_pages(url, params or None)

    # ------------------------------------------------------------------
    # Комментарии / отзывы (с пагинацией и датами)
    # ------------------------------------------------------------------

    def get_comments(self, company_id: str,
                     start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> List[Dict]:
        if not self._ensure_auth():
            return []

        url = f'{self.base_url}/comments/{company_id}/'
        params = {}
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date

        return self._get_all_pages(url, params or None)

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
        return result.get('data', []) if result else None

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
