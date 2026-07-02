"""Source-agnostic contract consumed by the ETL pipeline.

`sync_pipeline` historically depended on `YClientsAPI` directly. To onboard other
source systems (private vendor APIs, file exports) without touching the pipeline,
every source exposes the same read methods. `SyncSource` documents that contract.

The protocol is structural: `YClientsAPI` already satisfies it without inheritance,
and so does any new adapter (e.g. the file-import client) that returns data in the
same dict shapes the pipeline expects.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class SyncSource(Protocol):
    """Read-only source of salon data in YClients-compatible dict shapes."""

    def authenticate(self) -> bool: ...

    def get_groups(self) -> Optional[list[dict]]: ...

    def get_service_categories(self, company_id: str) -> Optional[list[dict]]: ...

    def get_services(self, company_id: str, staff_id: Optional[int] = None,
                     category_id: Optional[int] = None) -> Optional[list[dict]]: ...

    def get_positions(self, company_id: str) -> Optional[list[dict]]: ...

    def get_staff(self, company_id: str) -> Optional[list[dict]]: ...

    def get_clients(self, company_id: str) -> list[dict]: ...

    def get_accounts(self, company_id: str) -> Optional[list[dict]]: ...

    def get_storages(self, company_id: str) -> Optional[list[dict]]: ...

    def get_good_categories(self, company_id: str) -> Optional[list[dict]]: ...

    def get_goods(self, company_id: str) -> list[dict]: ...

    def get_records(self, company_id: str, start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> Optional[list[dict]]: ...

    def get_financial_transactions(self, company_id: str, start_date: Optional[str] = None,
                                   end_date: Optional[str] = None) -> Optional[list[dict]]: ...

    def get_goods_transactions(self, company_id: str, start_date: Optional[str] = None,
                               end_date: Optional[str] = None) -> Optional[list[dict]]: ...

    def get_comments(self, company_id: str, start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> Optional[list[dict]]: ...

    def get_staff_schedule(self, company_id: str, start_date: Optional[str] = None,
                           end_date: Optional[str] = None) -> Optional[list[dict]]: ...

    def get_analytics_overall(self, company_id: str, date_from: Any = None,
                              date_to: Any = None) -> Optional[dict]: ...


__all__ = ['SyncSource']
