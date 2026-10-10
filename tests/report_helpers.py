"""Helpers for reading a report payload the way the page does: through its tables."""

from typing import Any


def table_rows(report: dict[str, Any], table_id: str) -> list[dict[str, Any]]:
    """Rows of the table `table_id`; raises if the report has no such table."""
    for table in report['tables']:
        if table['id'] == table_id:
            return table['rows']
    raise KeyError(f"{table_id!r} is not a table of {report.get('report_id')!r}: {[t['id'] for t in report['tables']]}")


class SummaryResponse:
    """`GET /dashboard/bundle` seen as a summary-only response: `json()['data']` is the Overview summary.

    The SPA reads the summary from the bundle, so the Overview contract is tested through it.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self.status_code = response.status_code
        self.text = response.text
        self.headers = response.headers

    def json(self) -> Any:
        body = self._response.json()
        if self.status_code == 200:
            return {**body, 'data': body['data']['summary']}
        return body


async def get_summary(client: Any, **kwargs: Any) -> SummaryResponse:
    return SummaryResponse(await client.get('/dashboard/bundle', **kwargs))
