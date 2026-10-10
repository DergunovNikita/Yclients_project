from datetime import datetime
from typing import Optional

import requests


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()

    @property
    def enabled(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled:
            return False

        url = f'https://api.telegram.org/bot{self._bot_token}/sendMessage'
        try:
            response = requests.post(
                url,
                json={
                    'chat_id': self._chat_id,
                    'text': message,
                    'disable_web_page_preview': True,
                },
                timeout=10,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            # Class name only: the exception text carries the request URL, which holds the bot token.
            print(f'! Telegram notification failed: {exc.__class__.__name__}')
            return False


def build_sync_message(
    mode: str,
    trigger_type: str,
    status: str,
    started_at: str,
    finished_at: str,
    log_path: str,
    warning_count: int = 0,
    error_message: Optional[str] = None,
) -> str:
    def _fmt(value: str | datetime | None) -> str:
        if value is None:
            return '-'
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    lines = [
        'YClients BI sync',
        f'Режим: {mode}',
        f'Триггер: {trigger_type}',
        f'Статус: {status}',
        f'Старт: {_fmt(started_at)}',
        f'Завершение: {_fmt(finished_at)}',
        f'Warnings: {warning_count}',
        f'Лог: {log_path}',
    ]
    if error_message:
        lines.append(f'Ошибка: {error_message[:400]}')
    return '\n'.join(lines)
