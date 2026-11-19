from copy import deepcopy

from sqlalchemy import select

from app.errors import AppError
from app.models import DemoCampaign


class DemoClient:
    """Explicit offline sandbox, persisted separately from live campaign state."""

    def __init__(self, sessions):
        self.sessions = sessions

    async def close(self):
        pass

    async def catalog(self):
        return {
            "domains": [{"id": 1, "name": "demo.example.invalid", "state": "active"}],
            "groups": [{"id": 1, "name": "FORTESTS"}],
            "traffic_sources": [{"id": 1, "name": "FORTESTS", "state": "active"}],
            "offers": [
                {"id": i, "name": name, "state": "active"}
                for i, name in [
                    (101, "Demo — Alpha"),
                    (102, "Demo — Beta"),
                    (103, "Demo — Gamma"),
                    (104, "Demo — Delta"),
                ]
            ],
        }

    async def campaigns(self):
        with self.sessions() as db:
            return [deepcopy(r.data["campaign"]) for r in db.scalars(select(DemoCampaign))]

    def _get(self, db, campaign_id):
        row = db.get(DemoCampaign, campaign_id)
        if not row:
            raise AppError(404, "Кампания не найдена.")
        return row

    async def campaign(self, campaign_id):
        with self.sessions() as db:
            return deepcopy(self._get(db, campaign_id).data["campaign"])

    async def streams(self, campaign_id):
        with self.sessions() as db:
            return deepcopy(self._get(db, campaign_id).data["streams"])

    async def create_campaign(self, data):
        with self.sessions() as db:
            row = DemoCampaign(data={"campaign": deepcopy(data), "streams": []})
            db.add(row)
            db.flush()
            payload = deepcopy(row.data)
            payload["campaign"].update(id=row.id, url=f"https://demo.example.invalid/{data['alias']}")
            row.data = payload
            db.commit()
            return deepcopy(payload["campaign"])

    async def create_stream(self, data):
        with self.sessions() as db:
            row = self._get(db, data["campaign_id"])
            payload = deepcopy(row.data)
            stream = {"offers": [], "filters": [], "landings": [], **deepcopy(data)}
            stream["id"] = row.id * 1000 + len(payload["streams"]) + 1
            payload["streams"].append(stream)
            row.data = payload
            db.commit()
            return stream

    async def enable_campaign(self, campaign_id):
        with self.sessions() as db:
            row = self._get(db, campaign_id)
            payload = deepcopy(row.data)
            payload["campaign"]["state"] = "active"
            row.data = payload
            db.commit()
            return payload["campaign"]

    async def archive_campaign(self, campaign_id):
        with self.sessions() as db:
            row = self._get(db, campaign_id)
            db.delete(row)
            db.commit()

    async def update_offers(self, stream_id, offers):
        with self.sessions() as db:
            row = self._get(db, stream_id // 1000)
            payload = deepcopy(row.data)
            for stream in payload["streams"]:
                if stream["id"] == stream_id:
                    stream["offers"] = deepcopy(offers)
                    row.data = payload
                    db.commit()
                    return stream
            raise AppError(404, "Поток не найден.")
