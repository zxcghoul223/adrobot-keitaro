import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import make_database
from app.demo import DemoClient
from app.errors import AppError
from app.models import Base, CampaignState, Operation
from app.schemas import CreateCampaign
from app.services import CampaignService


class FailingProvisioner(DemoClient):
    """Persist real sandbox state and inject failures at integration boundaries."""

    def __init__(self, sessions, *, fail_at=None, fail_rollback=False):
        super().__init__(sessions)
        self.fail_at = fail_at
        self.fail_rollback = fail_rollback
        self.calls = []
        self.stream_count = 0

    async def create_campaign(self, data):
        self.calls.append(("create", deepcopy(data)))
        if self.fail_at == "create_rejected":
            raise AppError(502, "Creation rejected by tracker")
        campaign = await super().create_campaign(data)
        if self.fail_at == "create_ambiguous":
            # The remote write landed but its response/ID never reached the caller.
            raise AppError(502, "Creation response timed out", uncertain=True)
        return campaign

    async def create_stream(self, data):
        self.stream_count += 1
        self.calls.append(("stream", deepcopy(data)))
        assert (await self.campaign(data["campaign_id"]))["state"] == "disabled"
        if self.stream_count == 2 and self.fail_at == "second_stream":
            raise AppError(502, "Second stream rejected by tracker")
        return await super().create_stream(data)

    async def enable_campaign(self, campaign_id):
        self.calls.append(("enable", campaign_id))
        assert len(await self.streams(campaign_id)) == 2
        assert (await self.campaign(campaign_id))["state"] == "disabled"
        if self.fail_at == "enable":
            raise AppError(502, "Activation rejected by tracker")
        return await super().enable_campaign(campaign_id)

    async def archive_campaign(self, campaign_id):
        self.calls.append(("archive", campaign_id))
        if self.fail_rollback:
            raise AppError(502, "Archive request failed", uncertain=True)
        return await super().archive_campaign(campaign_id)


@pytest.fixture
def provisioner(tmp_path):
    engine, sessions = make_database(f"sqlite:///{(tmp_path / 'creation.db').as_posix()}")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        app_mode="demo",
        database_url=str(engine.url),
        campaign_prefix="test_Unit_",
    )

    def make(**faults):
        upstream = FailingProvisioner(sessions, **faults)
        return CampaignService(settings, sessions, upstream), upstream, sessions

    yield make
    engine.dispose()


def request(**overrides):
    return CreateCampaign(
        **{
            "name": "campaign",
            "country_code": "AU",
            "offer_id": 101,
            "domain_id": 1,
            "group_id": 1,
            "traffic_source_id": 1,
            **overrides,
        }
    )


def journal(sessions):
    with sessions() as db:
        return list(db.scalars(select(Operation).order_by(Operation.id)))


def test_creation_provisions_disabled_then_enables_after_both_streams(provisioner):
    service, upstream, sessions = provisioner()
    result = asyncio.run(service.create(request()))
    campaign_id = result["campaign"]["id"]
    assert result["campaign"]["name"] == "test_Unit_campaign"
    assert [event for event, _ in upstream.calls] == ["create", "stream", "stream", "enable"]
    creation = upstream.calls[0][1]
    assert creation["state"] == "disabled"
    assert creation["type"] == "position"
    assert creation["domain_id"] == 1
    assert str(creation["group_id"]) == "1"
    assert creation["traffic_source_id"] == 1
    streams = asyncio.run(upstream.streams(campaign_id))
    assert [stream["position"] for stream in streams] == [1, 2]
    assert streams[0]["filters"] == [{"name": "country", "mode": "accept", "payload": ["AU"]}]
    assert streams[0]["action_payload"] == "https://www.google.com/"
    assert streams[1]["filters"] == []
    assert streams[1]["offers"] == [{"offer_id": 101, "share": 100, "state": "active"}]
    assert asyncio.run(upstream.campaign(campaign_id))["state"] == "active"
    with sessions() as db:
        saved = db.scalar(select(CampaignState))
        assert saved.remote_id == campaign_id
        assert len(saved.data["streams"]) == 2
    (operation,) = journal(sessions)
    assert (operation.kind, operation.status, operation.remote_id) == ("create", "succeeded", campaign_id)


@pytest.mark.parametrize("fail_at", ["second_stream", "enable"])
def test_known_provisioning_failure_archives_incomplete_campaign(provisioner, fail_at):
    service, upstream, sessions = provisioner(fail_at=fail_at)
    with pytest.raises(AppError) as error:
        asyncio.run(service.create(request()))
    assert error.value.status == 502
    events = [event for event, _ in upstream.calls]
    assert events.count("create") == 1
    assert events.count("archive") == 1
    assert events.count("enable") == (1 if fail_at == "enable" else 0)
    assert asyncio.run(upstream.campaigns()) == []
    (operation,) = journal(sessions)
    assert operation.status == "rolled_back"
    assert operation.remote_id is not None
    with sessions() as db:
        assert db.scalar(select(CampaignState)) is None


def test_failed_compensation_is_journaled_with_campaign_to_check(provisioner):
    service, upstream, sessions = provisioner(fail_at="second_stream", fail_rollback=True)
    with pytest.raises(AppError) as error:
        asyncio.run(service.create(request()))
    (operation,) = journal(sessions)
    assert operation.status == "needs_attention"
    assert operation.remote_id is not None
    assert f"#{operation.remote_id}" in error.value.message
    remaining = asyncio.run(upstream.campaigns())
    assert len(remaining) == 1
    assert remaining[0]["state"] == "disabled"
    assert [event for event, _ in upstream.calls].count("archive") == 1


def test_ambiguous_create_without_id_is_not_retried_or_reported_as_rolled_back(provisioner):
    service, upstream, sessions = provisioner(fail_at="create_ambiguous")
    with pytest.raises(AppError) as error:
        asyncio.run(service.create(request()))
    assert error.value.status == 502
    assert [event for event, _ in upstream.calls] == ["create"]
    (operation,) = journal(sessions)
    assert operation.status == "needs_attention"
    assert operation.remote_id is None
    # This retained remote object is why blind retries would create duplicates.
    remaining = asyncio.run(upstream.campaigns())
    assert len(remaining) == 1
    assert remaining[0]["state"] == "disabled"
    assert "повтор" in error.value.message.lower()


def test_rejected_creation_without_remote_object_is_journaled_as_failed(provisioner):
    service, upstream, sessions = provisioner(fail_at="create_rejected")
    with pytest.raises(AppError):
        asyncio.run(service.create(request()))
    assert [event for event, _ in upstream.calls] == ["create"]
    assert asyncio.run(upstream.campaigns()) == []
    (operation,) = journal(sessions)
    assert operation.status == "failed"
    assert operation.remote_id is None


@pytest.mark.parametrize("field", ["offer_id", "domain_id", "group_id", "traffic_source_id"])
def test_invalid_catalog_reference_is_rejected_before_any_remote_mutation(provisioner, field):
    service, upstream, sessions = provisioner()
    with pytest.raises(AppError) as error:
        asyncio.run(service.create(request(**{field: 999_999})))
    assert error.value.status == 422
    assert upstream.calls == []
    assert journal(sessions) == []


def test_duplicate_name_does_not_create_a_second_campaign(provisioner):
    service, upstream, sessions = provisioner()
    asyncio.run(service.create(request()))
    with pytest.raises(AppError) as error:
        asyncio.run(service.create(request()))
    assert error.value.status == 409
    assert [event for event, _ in upstream.calls].count("create") == 1
    assert len(asyncio.run(upstream.campaigns())) == 1
    assert len(journal(sessions)) == 1
