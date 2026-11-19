import asyncio

import httpx

from app.errors import AppError


class KeitaroClient:
    """One pooled server-side client. Mutating requests are never retried."""

    def __init__(self, settings, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=settings.keitaro_url.rstrip("/") + "/admin_api/v1/",
            headers={
                "Api-Key": settings.keitaro_api_key.get_secret_value(),
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 Adrobot/1.0",
            },
            timeout=httpx.Timeout(settings.request_timeout),
            follow_redirects=False,
            transport=transport,
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.RequestError:
            raise AppError(
                502,
                "Keitaro недоступен или не ответил вовремя. Обновите данные перед повторной операцией.",
                uncertain=method != "GET",
            ) from None
        if not 200 <= response.status_code < 300:
            messages = {
                401: "Keitaro отклонил API-ключ.",
                403: "Нет доступа к ресурсу Keitaro.",
                404: "Ресурс не найден в Keitaro.",
                429: "Превышен лимит запросов Keitaro.",
            }
            raise AppError(
                502,
                messages.get(response.status_code, f"Keitaro отклонил запрос (HTTP {response.status_code})."),
                uncertain=method != "GET" and response.status_code >= 500,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise AppError(
                502, "Keitaro вернул ответ в неожиданном формате.", uncertain=method != "GET"
            ) from None

    async def catalog(self):
        paths = ["domains", "groups?type=campaigns", "traffic_sources", "offers"]
        values = await asyncio.gather(*(self.request("GET", p) for p in paths))
        if not all(isinstance(v, list) for v in values):
            raise AppError(502, "Некорректный формат справочника Keitaro.")
        return dict(zip(["domains", "groups", "traffic_sources", "offers"], values, strict=False))

    async def campaigns(self):
        items = []
        for offset in range(0, 100_000, 100):
            page = await self.request("GET", "campaigns", params={"offset": offset, "limit": 100})
            if not isinstance(page, list):
                raise AppError(502, "Некорректный список кампаний Keitaro.")
            items.extend(page)
            if len(page) < 100:
                return items
        raise AppError(502, "Слишком большой список кампаний; уточните область доступа API-ключа.")

    async def campaign(self, campaign_id):
        return await self.request("GET", f"campaigns/{campaign_id}")

    async def streams(self, campaign_id):
        return await self.request("GET", f"campaigns/{campaign_id}/streams")

    async def create_campaign(self, data):
        return await self.request("POST", "campaigns", json=data)

    async def create_stream(self, data):
        return await self.request("POST", "streams", json=data)

    async def enable_campaign(self, campaign_id):
        return await self.request("POST", f"campaigns/{campaign_id}/enable")

    async def archive_campaign(self, campaign_id):
        return await self.request("DELETE", f"campaigns/{campaign_id}")

    async def update_offers(self, stream_id, offers):
        return await self.request("PUT", f"streams/{stream_id}", json={"offers": offers})
