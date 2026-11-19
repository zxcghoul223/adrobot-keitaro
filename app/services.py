import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from uuid import uuid4

import pycountry
from sqlalchemy import select

from app.errors import AppError
from app.models import CampaignState, Operation
from app.weights import integer_share, rebalance, to_keitaro


def fingerprint(campaign, streams):
    def stream_config(stream):
        values = {key: stream.get(key) for key in keys}
        # Keitaro changes an unset action payload from null to "" on its first PUT.
        # Both mean no payload; a nonempty redirect/HTML payload remains significant.
        if values["action_payload"] is None:
            values["action_payload"] = ""
        return values

    keys = (
        "id",
        "name",
        "type",
        "schema",
        "position",
        "state",
        "action_type",
        "action_payload",
        "action_options",
        "filter_or",
        "filters",
        "landings",
        "offers",
        "triggers",
        "collect_clicks",
    )
    config = {
        "campaign": {
            k: campaign.get(k) for k in ("id", "type", "domain_id", "group_id", "traffic_source_id", "state")
        },
        "streams": [stream_config(s) for s in sorted(streams, key=lambda x: x["id"])],
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def dirty(stream):
    return stream["offers"] != stream["published"]


def campaign_url(campaign):
    """Older Keitaro versions return domain + alias instead of a ready campaign URL."""

    def valid_http_url(value):
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(c.isspace() for c in parsed.hostname)
            ):
                return None
            _ = parsed.port  # Also reject malformed port numbers.
            return parsed
        except ValueError:
            return None

    ready = campaign.get("url")
    if valid_http_url(ready):
        return ready
    domain, alias = campaign.get("domain"), campaign.get("alias")
    if not isinstance(domain, str) or not isinstance(alias, str) or not alias:
        return ""
    domain = domain.strip()
    parsed = valid_http_url(domain if "://" in domain else f"https://{domain}")
    if not parsed or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/{quote(alias, safe='')}"


def offer_values(offers):
    """Compare actual percentages, never normalize a malformed upstream configuration."""
    try:
        values = {}
        for offer in offers:
            offer_id = int(offer["offer_id"])
            share = integer_share(offer.get("share", 0))
            if offer_id in values:
                return None
            values[offer_id] = (offer.get("state", "active"), share)
        return values
    except (AppError, KeyError, TypeError, ValueError):
        return None


def same_offers(expected, actual):
    expected, actual = offer_values(expected), offer_values(actual)
    return expected is not None and actual is not None and expected == actual


class CampaignService:
    def __init__(self, settings, sessions, client):
        self.settings, self.sessions, self.client = settings, sessions, client
        self.locks = {}
        self.create_lock = asyncio.Lock()

    def lock(self, campaign_id):
        return self.locks.setdefault(campaign_id, asyncio.Lock())

    def owned(self, campaign):
        if not str(campaign.get("name", "")).startswith(self.settings.campaign_prefix):
            raise AppError(403, "Редактировать можно только кампании с вашим тестовым префиксом.")

    def row(self, db, campaign_id):
        return db.scalar(
            select(CampaignState).where(
                CampaignState.mode == self.settings.app_mode, CampaignState.remote_id == campaign_id
            )
        )

    def check_revision(self, row, revision):
        if revision is not None and revision != row.revision:
            raise AppError(409, "Черновик изменился в другой вкладке. Обновите страницу.")

    def view(self, row):
        self.owned({"name": row.name})
        result = deepcopy(row.data)
        result.update(id=row.remote_id, name=row.name, revision=row.revision)
        result["dirty"] = False
        for stream in result["streams"]:
            stream["dirty"] = dirty(stream)
            result["dirty"] |= stream["dirty"]
            stream["warning"] = (
                "Сумма активных долей в Keitaro отличается от 100%. "
                "Показаны фактические значения; изменение черновика перераспределит доли."
                if not stream["read_only"]
                and sum(o["share"] for o in stream["offers"] if o["state"] == "active") != 100
                else ""
            )
            stream.pop("published")
        return result

    def save(self, db, row):
        row.revision += 1
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return self.view(row)

    async def catalog(self):
        data = await self.client.catalog()
        for key, items in data.items():
            data[key] = [
                {"id": int(x["id"]), "name": x.get("name", str(x["id"]))}
                for x in items
                if x.get("state", "active") == "active"
            ]
        data["countries"] = sorted(
            [{"code": c.alpha_2, "name": c.name} for c in pycountry.countries], key=lambda x: x["name"]
        )
        return data

    async def campaigns(self, search=""):
        items = await self.client.campaigns()
        return {
            "items": [
                {"id": c["id"], "name": c["name"], "url": campaign_url(c)}
                for c in items
                if c.get("state") != "deleted"
                and c["name"].startswith(self.settings.campaign_prefix)
                and search.casefold() in c["name"].casefold()
            ]
        }

    async def get(self, campaign_id):
        with self.sessions() as db:
            row = self.row(db, campaign_id)
            if row:
                return self.view(row)
        return await self.fetch(campaign_id)

    async def fetch(self, campaign_id, revision=None, discard_draft=False):
        async with self.lock(campaign_id):
            with self.sessions() as db:
                row = self.row(db, campaign_id)
                if row:
                    self.check_revision(row, revision)
                    if any(dirty(s) for s in row.data["streams"]) and not discard_draft:
                        raise AppError(409, "Сначала опубликуйте или отмените черновик.")
                campaign, streams, catalog = await asyncio.gather(
                    self.client.campaign(campaign_id), self.client.streams(campaign_id), self.client.catalog()
                )
                self.owned(campaign)
                old = row.data if row else {"streams": []}
                data = self.merge(campaign, streams, catalog, old)
                if not row:
                    row = CampaignState(
                        mode=self.settings.app_mode,
                        remote_id=campaign_id,
                        name=campaign["name"],
                        revision=0,
                        data=data,
                        remote_revision="",
                    )
                    db.add(row)
                row.name, row.data = campaign["name"], data
                row.remote_revision = fingerprint(campaign, streams)
                return self.save(db, row)

    def merge(self, campaign, remote_streams, catalog, old):
        names = {o["id"]: o["name"] for o in catalog["offers"]}
        previous = {s["id"]: s for s in old["streams"]}
        result = []
        for remote in sorted(remote_streams, key=lambda s: s.get("position", 0)):
            # Published history is authoritative: an explicit refetch discards unpushed additions.
            history = deepcopy(previous.get(remote["id"], {}).get("published", []))
            by_id = {o["offer_id"]: o for o in history}
            incoming = remote.get("offers") or []
            values = offer_values(incoming)
            if values is None:
                raise AppError(502, "Keitaro вернул некорректные доли офферов.")
            total = sum(share for state, share in values.values() if state == "active")
            offers = []
            for raw in incoming:
                oid = int(raw["offer_id"])
                prior = by_id.pop(oid, {})
                share = values[oid][1] if raw.get("state", "active") == "active" else 0
                offers.append(
                    {
                        "offer_id": oid,
                        "name": names.get(oid, prior.get("name", f"Offer #{oid}")),
                        "share": share,
                        "state": "active" if raw.get("state", "active") == "active" else "removed",
                        "pinned": prior.get("pinned", False),
                        "pinned_share": prior.get("pinned_share", share),
                        "keitaro_state": raw.get("state", "active"),
                        "remote_disabled": raw.get("state", "active") != "active",
                        "remote_weight": raw.get("share", 0),
                    }
                )
            for prior in by_id.values():
                prior.update(state="removed", share=0, remote_disabled=False)
                offers.append(prior)
            stream = {
                "id": remote["id"],
                "name": remote["name"],
                "type": remote.get("type"),
                "schema": remote.get("schema"),
                "position": remote.get("position"),
                "read_only": not bool(incoming or history),
                "offers": offers,
                "published": deepcopy(offers),
                "remote_revision": fingerprint(campaign, [remote]),
                "warning": (
                    "Сумма активных долей в Keitaro отличается от 100%. "
                    "Показаны фактические значения; изменение черновика перераспределит доли."
                    if incoming and total != 100
                    else ""
                ),
            }
            result.append(stream)
        return {"url": campaign_url(campaign), "streams": result}

    async def create(self, request):
        async with self.create_lock:
            catalog = await self.catalog()
            for field, key in [
                ("domain_id", "domains"),
                ("group_id", "groups"),
                ("traffic_source_id", "traffic_sources"),
                ("offer_id", "offers"),
            ]:
                if getattr(request, field) not in {x["id"] for x in catalog[key]}:
                    raise AppError(422, f"Недоступное значение справочника: {field}.")
            name = request.name
            if not name.startswith(self.settings.campaign_prefix):
                name = self.settings.campaign_prefix + name
            if any(c["name"] == name for c in (await self.campaigns())["items"]):
                raise AppError(409, "Кампания с таким названием уже существует. Обновите список.")
            operation_id = self.operation("create")
            campaign_id = None
            try:
                campaign = await self.client.create_campaign(
                    {
                        "name": name,
                        "alias": uuid4().hex[:16],
                        "type": "position",
                        "state": "disabled",
                        "domain_id": request.domain_id,
                        "group_id": str(request.group_id),
                        "traffic_source_id": request.traffic_source_id,
                    }
                )
                campaign_id = campaign["id"]
                self.finish(operation_id, "running", campaign_id)
                common = {
                    "campaign_id": campaign_id,
                    "type": "regular",
                    "state": "active",
                    "action_type": "http",
                    "collect_clicks": True,
                    "filter_or": False,
                    "landings": [],
                }
                await self.client.create_stream(
                    {
                        **common,
                        "name": "GEO → Google",
                        "schema": "redirect",
                        "position": 1,
                        "action_payload": "https://www.google.com/",
                        "filters": [{"name": "country", "mode": "accept", "payload": [request.country_code]}],
                        "offers": [],
                    }
                )
                await self.client.create_stream(
                    {
                        **common,
                        "name": "Offers",
                        "schema": "landings",
                        "position": 2,
                        "filters": [],
                        "offers": [{"offer_id": request.offer_id, "share": 100, "state": "active"}],
                    }
                )
                await self.client.enable_campaign(campaign_id)
            except AppError as error:
                status = "needs_attention" if error.uncertain else "failed"
                if campaign_id is not None:
                    try:
                        await self.client.archive_campaign(campaign_id)
                        status = "rolled_back"
                    except AppError:
                        status = "needs_attention"
                self.finish(operation_id, status, campaign_id, error.message)
                suffix = (
                    f" Проверьте кампанию #{campaign_id} в Keitaro."
                    if status == "needs_attention" and campaign_id
                    else " Проверьте список Keitaro перед повторным созданием."
                    if status == "needs_attention"
                    else " Неполная кампания помещена в архив."
                    if status == "rolled_back"
                    else ""
                )
                raise AppError(error.status, error.message + suffix) from None
            self.finish(operation_id, "succeeded", campaign_id)
            result = await self.fetch(campaign_id)
            return {"campaign": {k: result[k] for k in ("id", "name", "url")}, "streams": result["streams"]}

    def operation(self, kind):
        with self.sessions() as db:
            operation = Operation(mode=self.settings.app_mode, kind=kind)
            db.add(operation)
            db.commit()
            return operation.id

    def finish(self, operation_id, status, remote_id=None, message=""):
        with self.sessions() as db:
            operation = db.get(Operation, operation_id)
            operation.status, operation.remote_id, operation.message = status, remote_id, message
            db.commit()

    async def change(self, campaign_id, stream_id, request):
        async with self.lock(campaign_id):
            with self.sessions() as db:
                row = self.row(db, campaign_id)
                if not row:
                    raise AppError(404, "Сначала загрузите кампанию из Keitaro.")
                self.owned({"name": row.name})
                self.check_revision(row, request.revision)
                data = deepcopy(row.data)
                stream = self.stream(data, stream_id)
                offers = stream["offers"]
                item = next((o for o in offers if o["offer_id"] == request.offer_id), None)
                if request.action == "add":
                    if item:
                        raise AppError(422, "Оффер уже известен потоку; используйте «Вернуть».")
                    catalog = await self.catalog()
                    item = next((o for o in catalog["offers"] if o["id"] == request.offer_id), None)
                    if not item:
                        raise AppError(422, "Оффер недоступен в Keitaro.")
                    offers.append(
                        {
                            "offer_id": item["id"],
                            "name": item["name"],
                            "share": 0,
                            "state": "active",
                            "pinned": False,
                            "pinned_share": 0,
                            "keitaro_state": "active",
                        }
                    )
                elif not item:
                    raise AppError(404, "Оффер не найден в этом потоке.")
                elif request.action == "remove":
                    item["state"] = "removed"
                    item["remote_disabled"] = False
                elif request.action == "restore":
                    catalog = await self.catalog()
                    if item["offer_id"] not in {o["id"] for o in catalog["offers"]}:
                        raise AppError(422, "Этот оффер больше недоступен в Keitaro.")
                    item["state"], item["keitaro_state"] = "active", "active"
                    item["remote_disabled"] = False
                elif request.action == "pin":
                    if item["state"] != "active" or request.share is None:
                        raise AppError(422, "Укажите долю активного оффера для закрепления.")
                    item["pinned"], item["pinned_share"] = True, float(request.share)
                elif request.action == "unpin":
                    item["pinned"] = False
                rebalance(offers)
                stream["warning"] = ""
                row.data = data
                return self.save(db, row)

    def stream(self, data, stream_id):
        stream = next((s for s in data["streams"] if s["id"] == stream_id), None)
        if stream is None:
            raise AppError(404, "Поток не принадлежит этой кампании.")
        if stream["read_only"]:
            raise AppError(422, "Поток без офферов доступен только для просмотра.")
        return stream

    async def cancel(self, campaign_id, stream_id, revision):
        async with self.lock(campaign_id):
            with self.sessions() as db:
                row = self.row(db, campaign_id)
                if not row:
                    raise AppError(404, "Кампания не загружена.")
                self.owned({"name": row.name})
                self.check_revision(row, revision)
                data = deepcopy(row.data)
                stream = self.stream(data, stream_id)
                stream["offers"] = deepcopy(stream["published"])
                row.data = data
                return self.save(db, row)

    async def publish(self, campaign_id, stream_id, revision):
        async with self.lock(campaign_id):
            with self.sessions() as db:
                row = self.row(db, campaign_id)
                if not row:
                    raise AppError(404, "Кампания не загружена.")
                self.check_revision(row, revision)
                data = deepcopy(row.data)
                stream = self.stream(data, stream_id)
                if not dirty(stream):
                    return self.view(row)
                campaign, streams = await asyncio.gather(
                    self.client.campaign(campaign_id), self.client.streams(campaign_id)
                )
                self.owned(campaign)
                remote = next((s for s in streams if s["id"] == stream_id), None)
                expected_revision = stream.get("remote_revision")
                if expected_revision is None and fingerprint(campaign, streams) == row.remote_revision:
                    # Existing databases created before per-stream conflict baselines are compatible
                    # only when the complete old campaign baseline still matches.
                    expected_revision = fingerprint(campaign, [remote]) if remote else None
                if remote is None or fingerprint(campaign, [remote]) != expected_revision:
                    raise AppError(
                        409,
                        "Кампания изменена в Keitaro. Загрузите потоки заново; "
                        "текущий черновик не опубликован.",
                    )
                rebalance(stream["offers"])
                desired_offers = to_keitaro(stream["offers"])
                operation_id = self.operation("publish")
                write_completed = False
                try:
                    await self.client.update_offers(stream_id, desired_offers)
                    write_completed = True
                    fresh_campaign, fresh_streams = await asyncio.gather(
                        self.client.campaign(campaign_id), self.client.streams(campaign_id)
                    )
                    self.owned(fresh_campaign)
                    fresh = next((s for s in fresh_streams if s["id"] == stream_id), None)
                    if (
                        fresh is None
                        or not same_offers(desired_offers, fresh.get("offers") or [])
                        or fingerprint(campaign, [{**remote, "offers": []}])
                        != fingerprint(fresh_campaign, [{**fresh, "offers": []}])
                    ):
                        raise AppError(
                            409,
                            "Состояние Keitaro после записи отличается от отправленного. "
                            "Проверьте поток и загрузите его заново; черновик сохранён.",
                            uncertain=True,
                        )
                except AppError as error:
                    self.finish(
                        operation_id,
                        "needs_attention" if write_completed or error.uncertain else "failed",
                        campaign_id,
                        error.message,
                    )
                    if write_completed and not error.uncertain:
                        raise AppError(
                            error.status,
                            error.message + " Запись могла сохраниться. Проверьте поток перед повтором.",
                            uncertain=True,
                        ) from None
                    raise
                # Only the verified target adopts a fresh baseline. Other drafts retain their own
                # baseline and still detect changes made externally while this request was running.
                actual = offer_values(fresh["offers"])
                for offer in stream["offers"]:
                    if offer["state"] == "active":
                        offer["share"] = actual[offer["offer_id"]][1]
                    elif offer.get("remote_disabled"):
                        offer["remote_weight"] = next(
                            o["share"] for o in fresh["offers"] if o["offer_id"] == offer["offer_id"]
                        )
                stream["published"] = deepcopy(stream["offers"])
                stream["remote_revision"] = fingerprint(fresh_campaign, [fresh])
                row.data = data
                row.remote_revision = fingerprint(fresh_campaign, fresh_streams)
                result = self.save(db, row)
                self.finish(operation_id, "succeeded", campaign_id)
                return result
