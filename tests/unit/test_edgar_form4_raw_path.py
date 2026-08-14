from __future__ import annotations

import httpx
import pytest

from auspex.providers.edgar import EdgarClient


@pytest.mark.asyncio
async def test_form4_uses_raw_xml_basename_not_xsl_rendered_path() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            text="<ownershipDocument />",
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = EdgarClient(
        base_url="https://data.sec.test",
        www_base_url="https://www.sec.test",
        user_agent="Auspex test",
        client=http,
        rate_limit_per_second=1000,
    )

    content = await client.get_form4_xml(
        "0000917273",
        "000119312526342968",
        "xslF345X06/ownership.xml",
    )

    assert content == "<ownershipDocument />"
    assert requested == [
        "https://www.sec.test/Archives/edgar/data/917273/000119312526342968/ownership.xml"
    ]
    await http.aclose()
