"""Data source adapter boundary for tenant onboarding and sync."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Company, Group, PortalBranch, YClientsCredential
from portal_tenant_ownership import reassign_branch_from_admin_only_tenant
from yclients_api import YClientsAPI
from yclients_credentials import decrypt_secret

SOURCE_YCLIENTS = 'yclients'
SUPPORTED_SOURCE_TYPES = (SOURCE_YCLIENTS,)


@dataclass(frozen=True)
class DataSourceBranch:
    group_id: int
    group_title: str
    company_id: int
    title: str

    def as_payload(self) -> dict:
        return {
            'group_id': self.group_id,
            'group_title': self.group_title,
            'company_id': self.company_id,
            'title': self.title,
        }


class DataSourceAdapter(ABC):
    source_type: str

    @abstractmethod
    def authenticate(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_branches(self) -> list[DataSourceBranch]:
        raise NotImplementedError

    @abstractmethod
    async def materialize_branches(
        self,
        db: AsyncSession,
        portal_account_id: int,
        company_ids: list[int],
    ) -> list[int]:
        raise NotImplementedError

    @abstractmethod
    def build_sync_client(self):
        raise NotImplementedError


class YClientsDataSourceAdapter(DataSourceAdapter):
    source_type = SOURCE_YCLIENTS

    def __init__(
        self,
        partner_token: str,
        login: str,
        password: str,
        *,
        api_factory: Callable[..., YClientsAPI] | None = None,
        **api_kwargs,
    ):
        self.partner_token = partner_token
        self.login = login
        self.password = password
        self._api_factory = api_factory or YClientsAPI
        self._api_kwargs = api_kwargs
        self._api = None

    @classmethod
    def from_credential(cls, credential: YClientsCredential, **api_kwargs) -> 'YClientsDataSourceAdapter':
        return cls(
            decrypt_secret(credential.partner_token_encrypted),
            decrypt_secret(credential.login_encrypted),
            decrypt_secret(credential.password_encrypted),
            **api_kwargs,
        )

    def build_sync_client(self):
        if self._api is None:
            self._api = self._api_factory(
                self.partner_token,
                self.login,
                self.password,
                **self._api_kwargs,
            )
        return self._api

    def authenticate(self) -> bool:
        return bool(self.build_sync_client().authenticate())

    def list_branches(self) -> list[DataSourceBranch]:
        groups = self.build_sync_client().get_groups() or []
        items: list[DataSourceBranch] = []
        for group_data in groups:
            group_id = group_data.get('id')
            if group_id is None:
                continue
            for company_data in group_data.get('companies') or []:
                company_id = company_data.get('id')
                if company_id is None:
                    continue
                items.append(
                    DataSourceBranch(
                        group_id=int(group_id),
                        group_title=group_data.get('title', ''),
                        company_id=int(company_id),
                        title=company_data.get('title', ''),
                    )
                )
        return items

    async def materialize_branches(
        self,
        db: AsyncSession,
        portal_account_id: int,
        company_ids: list[int],
    ) -> list[int]:
        available = {item.company_id: item for item in self.list_branches()}
        unknown = [cid for cid in company_ids if cid not in available]
        if unknown:
            raise HTTPException(status_code=400, detail=f'Companies not available for credential: {unknown}')

        for cid in company_ids:
            meta = available[cid]
            group = await db.get(Group, meta.group_id)
            if group is None:
                db.add(Group(id=meta.group_id, title=meta.group_title))
            else:
                group.title = meta.group_title or group.title

            company = await db.get(Company, cid)
            if company is None:
                db.add(Company(id=cid, title=meta.title, group_id=meta.group_id))
            else:
                company.title = meta.title or company.title
                company.group_id = meta.group_id

            existing_branch = (
                await db.execute(select(PortalBranch).where(PortalBranch.company_id == cid))
            ).scalar_one_or_none()
            if existing_branch is None:
                db.add(PortalBranch(portal_account_id=portal_account_id, company_id=cid))
            elif not await reassign_branch_from_admin_only_tenant(db, existing_branch, portal_account_id):
                raise HTTPException(status_code=409, detail=f'Company {cid} already linked to another tenant')

        await db.flush()
        return company_ids


def normalize_source_type(source_type: str | None) -> str:
    normalized = (source_type or SOURCE_YCLIENTS).strip().lower()
    if normalized not in SUPPORTED_SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported source_type. Allowed: {", ".join(SUPPORTED_SOURCE_TYPES)}',
        )
    return normalized


def adapter_from_payload(
    source_type: str | None,
    *,
    partner_token: str,
    login: str,
    password: str,
    **api_kwargs,
) -> DataSourceAdapter:
    normalized = normalize_source_type(source_type)
    if normalized == SOURCE_YCLIENTS:
        return YClientsDataSourceAdapter(partner_token, login, password, **api_kwargs)
    raise HTTPException(status_code=400, detail=f'Unsupported source_type: {normalized}')


def adapter_from_credential(
    source_type: str | None,
    credential: YClientsCredential,
    **api_kwargs,
) -> DataSourceAdapter:
    normalized = normalize_source_type(source_type)
    if normalized == SOURCE_YCLIENTS:
        return YClientsDataSourceAdapter.from_credential(credential, **api_kwargs)
    raise HTTPException(status_code=400, detail=f'Unsupported source_type: {normalized}')
