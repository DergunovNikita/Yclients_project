"""Data source adapter boundary for tenant onboarding and sync."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from file_import import SOURCE_FILE_IMPORT, FileImportSyncClient, ImportPayload
from mapping_profiles import get_profile
from models import Company, Group, PortalBranch, YClientsCredential
from portal_tenant_ownership import reassign_branch_from_admin_only_tenant
from yclients_api import YClientsAPI
from yclients_credentials import decrypt_secret

SOURCE_YCLIENTS = 'yclients'
SUPPORTED_SOURCE_TYPES = (SOURCE_YCLIENTS,)

# Sources authenticated with partner_token/login/password (credential onboarding).
CREDENTIAL_SOURCE_TYPES = (SOURCE_YCLIENTS,)


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

        materialized_company_ids: list[int] = []
        for cid in company_ids:
            meta = available[cid]
            group = (
                await db.execute(
                    select(Group).where(
                        Group.portal_account_id == portal_account_id,
                        Group.external_id == meta.group_id,
                    )
                )
            ).scalar_one_or_none()
            if group is None:
                legacy_group = await db.get(Group, meta.group_id)
                if legacy_group is not None and legacy_group.external_id is None and (
                    legacy_group.portal_account_id is None or legacy_group.portal_account_id == portal_account_id
                ):
                    group = legacy_group
                    group.portal_account_id = portal_account_id
                    group.external_id = meta.group_id
                    group.title = meta.group_title or group.title
                else:
                    group = Group(
                        portal_account_id=portal_account_id,
                        external_id=meta.group_id,
                        title=meta.group_title,
                    )
                    db.add(group)
                    await db.flush()
            else:
                group.title = meta.group_title or group.title

            company = (
                await db.execute(
                    select(Company).where(
                        Company.portal_account_id == portal_account_id,
                        Company.source_type == self.source_type,
                        Company.external_id == cid,
                    )
                )
            ).scalar_one_or_none()
            if company is None:
                legacy_company = await db.get(Company, cid)
                if legacy_company is not None and legacy_company.external_id is None and (
                    legacy_company.portal_account_id is None or legacy_company.portal_account_id == portal_account_id
                ):
                    company = legacy_company
                    company.portal_account_id = portal_account_id
                    company.external_id = cid
                    company.source_type = self.source_type
                    company.title = meta.title or company.title
                    company.group_id = group.id
                else:
                    company = Company(
                        portal_account_id=portal_account_id,
                        external_id=cid,
                        source_type=self.source_type,
                        title=meta.title,
                        group_id=group.id,
                    )
                    db.add(company)
                    await db.flush()
            else:
                company.title = meta.title or company.title
                company.group_id = group.id

            existing_branch = (
                await db.execute(select(PortalBranch).where(PortalBranch.company_id == company.id))
            ).scalar_one_or_none()
            if existing_branch is None:
                db.add(PortalBranch(portal_account_id=portal_account_id, company_id=company.id))
            elif not await reassign_branch_from_admin_only_tenant(db, existing_branch, portal_account_id):
                raise HTTPException(status_code=409, detail=f'Company {cid} already linked to another tenant')
            materialized_company_ids.append(int(company.id))

        await db.flush()
        return materialized_company_ids


class FileImportDataSourceAdapter(DataSourceAdapter):
    """Adapter for exports uploaded by the salon (no remote API).

    Branch discovery does not apply — the company is created from the upload
    form, not fetched remotely. Only `build_sync_client` participates in sync.
    """

    source_type = SOURCE_FILE_IMPORT

    def __init__(self, profile_name: str, payload: ImportPayload):
        self.profile = get_profile(profile_name)
        self.payload = payload

    def build_sync_client(self) -> FileImportSyncClient:
        return FileImportSyncClient(self.profile, self.payload)

    def authenticate(self) -> bool:
        return True

    def list_branches(self) -> list[DataSourceBranch]:
        raise HTTPException(
            status_code=400,
            detail='file_import has no remote branches; create the company from the upload form',
        )

    async def materialize_branches(self, db: AsyncSession, portal_account_id: int,
                                   company_ids: list[int]) -> list[int]:
        raise HTTPException(
            status_code=400,
            detail='file_import branches are created via the upload flow, not discovered',
        )


def file_import_adapter(profile_name: str, payload: ImportPayload) -> FileImportDataSourceAdapter:
    return FileImportDataSourceAdapter(profile_name, payload)


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
    if normalized == SOURCE_FILE_IMPORT:
        raise HTTPException(
            status_code=400,
            detail='file_import is onboarded via file upload, not credentials',
        )
    raise HTTPException(status_code=400, detail=f'Unsupported source_type: {normalized}')


def adapter_from_credential(
    source_type: str | None,
    credential: YClientsCredential,
    **api_kwargs,
) -> DataSourceAdapter:
    normalized = normalize_source_type(source_type)
    if normalized == SOURCE_YCLIENTS:
        return YClientsDataSourceAdapter.from_credential(credential, **api_kwargs)
    if normalized == SOURCE_FILE_IMPORT:
        raise HTTPException(
            status_code=400,
            detail='file_import is onboarded via file upload, not credentials',
        )
    raise HTTPException(status_code=400, detail=f'Unsupported source_type: {normalized}')
