from copy import deepcopy
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from app.config import Settings
from app.errors import AppError
from app.main import create_app
from app.models import CampaignState, DemoCampaign, Operation
from app.services import campaign_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path, monkeypatch):
    database_url = f"sqlite:///{(tmp_path / 'editor.db').as_posix()}"
    monkeypatch.setenv("APP_MODE", "demo")
    monkeypatch.setenv("DATABASE_URL", database_url)
    settings = Settings(
        _env_file=None,
        app_mode="demo",
        database_url=database_url,
        campaign_prefix="test_Unit_",
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    app = create_app(settings)
    with TestClient(app) as client:
        client.settings = settings
        client.migrations = config
        yield client


def create(api, name="campaign"):
    response = api.post(
        "/api/campaigns",
        json={
            "name": name,
            "country_code": "au",
            "offer_id": 101,
            "domain_id": 1,
            "group_id": 1,
            "traffic_source_id": 1,
        },
    )
    assert response.status_code == 201, response.text
    campaign_id = response.json()["campaign"]["id"]
    response = api.get(f"/api/campaigns/{campaign_id}")
    assert response.status_code == 200, response.text
    return response.json()


def editable(campaign):
    return next(stream for stream in campaign["streams"] if not stream["read_only"])


def change(api, campaign, action, offer_id, **fields):
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/draft",
        json={"revision": campaign["revision"], "action": action, "offer_id": offer_id, **fields},
    )
    assert response.status_code == 200, response.text
    return response.json()


def publish(api, campaign):
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def remote_stream(api, campaign):
    streams = api.portal.call(api.app.state.service.client.streams, campaign["id"])
    return next(stream for stream in streams if stream["id"] == editable(campaign)["id"])


def shares(campaign):
    return {
        offer["offer_id"]: offer["share"]
        for offer in editable(campaign)["offers"]
        if offer["state"] == "active"
    }


def test_migrated_database_starts_and_contains_persistent_state(api):
    assert api.get("/healthz").json() == {"status": "ok", "mode": "demo"}
    assert {"campaign_states", "operations", "demo_campaigns", "alembic_version"} <= set(
        inspect(api.app.state.engine).get_table_names()
    )
    campaign = create(api)
    with api.app.state.sessions() as db:
        assert db.scalar(select(CampaignState)).remote_id == campaign["id"]
        assert db.scalar(select(DemoCampaign)).id == campaign["id"]
        operation = db.scalar(select(Operation))
        assert operation.kind == "create"
        assert operation.status == "succeeded"


def test_creator_builds_ordered_geo_and_fallback_streams(api):
    campaign = create(api)
    assert campaign["name"] == "test_Unit_campaign"
    remote = api.portal.call(api.app.state.service.client.campaign, campaign["id"])
    assert remote["domain_id"] == 1
    assert str(remote["group_id"]) == "1"
    assert remote["traffic_source_id"] == 1
    assert remote["type"] == "position"
    assert remote["state"] == "active"
    streams = api.portal.call(api.app.state.service.client.streams, campaign["id"])
    assert len(streams) == 2
    geo, fallback = sorted(streams, key=lambda stream: stream["position"])
    assert geo["schema"] == "redirect"
    assert geo["action_payload"] == "https://www.google.com/"
    assert geo["filters"] == [{"name": "country", "mode": "accept", "payload": ["AU"]}]
    assert geo["offers"] == []
    assert fallback["filters"] == []
    assert fallback["offers"] == [{"offer_id": 101, "share": 100, "state": "active"}]


def test_creator_does_not_repeat_an_existing_prefix_or_duplicate_name(api):
    campaign = create(api, "test_Unit_existing")
    assert campaign["name"] == "test_Unit_existing"
    response = api.post(
        "/api/campaigns",
        json={
            "name": "existing",
            "country_code": "AU",
            "offer_id": 101,
            "domain_id": 1,
            "group_id": 1,
            "traffic_source_id": 1,
        },
    )
    assert response.status_code == 409
    assert len(api.get("/api/campaigns").json()["items"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("country_code", "ZZ"),
        ("offer_id", 999),
        ("domain_id", 999),
        ("group_id", 999),
        ("traffic_source_id", 999),
        ("name", "   "),
    ],
)
def test_invalid_creation_does_not_write_a_remote_campaign(api, field, value):
    payload = {
        "name": "bad",
        "country_code": "AU",
        "offer_id": 101,
        "domain_id": 1,
        "group_id": 1,
        "traffic_source_id": 1,
    }
    payload[field] = value
    response = api.post("/api/campaigns", json=payload)
    assert response.status_code == 422
    assert api.get("/api/campaigns").json()["items"] == []


def test_add_is_a_persistent_draft_until_publish(api):
    campaign = change(api, create(api), "add", 102)
    assert campaign["dirty"] is True
    assert shares(campaign) == {101: 50, 102: 50}
    assert [offer["offer_id"] for offer in remote_stream(api, campaign)["offers"]] == [101]
    app_again = create_app(api.settings)
    with TestClient(app_again) as reopened:
        persisted = reopened.get(f"/api/campaigns/{campaign['id']}").json()
    assert persisted["revision"] == campaign["revision"]
    assert persisted["dirty"] is True
    assert shares(persisted) == {101: 50, 102: 50}
    campaign = publish(api, campaign)
    assert campaign["dirty"] is False
    assert [offer["offer_id"] for offer in remote_stream(api, campaign)["offers"]] == [101, 102]


def test_cancel_restores_the_published_state_without_touching_keitaro(api):
    campaign = change(api, create(api), "add", 102)
    before = remote_stream(api, campaign)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/cancel",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["dirty"] is False
    assert shares(restored) == {101: 100}
    assert restored["revision"] > campaign["revision"]
    assert remote_stream(api, restored) == before


def test_archive_survives_publish_and_fetch_and_can_be_restored(api):
    campaign = publish(api, change(api, create(api), "add", 102))
    campaign = change(api, campaign, "remove", 101)
    removed = next(offer for offer in editable(campaign)["offers"] if offer["offer_id"] == 101)
    assert removed["state"] == "removed"
    assert removed["share"] == 0
    assert len(remote_stream(api, campaign)["offers"]) == 2
    campaign = publish(api, campaign)
    assert [offer["offer_id"] for offer in remote_stream(api, campaign)["offers"]] == [102]
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = response.json()
    assert (
        next(offer for offer in editable(campaign)["offers"] if offer["offer_id"] == 101)["state"]
        == "removed"
    )
    campaign = change(api, campaign, "restore", 101)
    assert shares(campaign) == {102: 50, 101: 50}
    campaign = publish(api, campaign)
    assert {offer["offer_id"] for offer in remote_stream(api, campaign)["offers"]} == {101, 102}


def test_fetch_marks_an_externally_deleted_offer_as_removed(api):
    campaign = publish(api, change(api, create(api), "add", 102))
    api.portal.call(
        api.app.state.service.client.update_offers,
        editable(campaign)["id"],
        [{"offer_id": 101, "share": 100, "state": "active"}],
    )
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = response.json()
    assert shares(campaign) == {101: 100}
    assert (
        next(offer for offer in editable(campaign)["offers"] if offer["offer_id"] == 102)["state"]
        == "removed"
    )


def test_pinned_percentages_survive_other_changes_publish_fetch_and_restart(api):
    campaign = change(api, create(api), "add", 102)
    campaign = change(api, campaign, "add", 103)
    campaign = change(api, campaign, "pin", 101, share=25)
    assert shares(campaign) == {101: 25, 102: 38, 103: 37}
    campaign = publish(api, campaign)
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = response.json()
    assert shares(campaign) == {101: 25, 102: 38, 103: 37}
    assert next(offer for offer in editable(campaign)["offers"] if offer["offer_id"] == 101)["pinned"]
    with TestClient(create_app(api.settings)) as reopened:
        persisted = reopened.get(f"/api/campaigns/{campaign['id']}").json()
    assert shares(persisted) == {101: 25, 102: 38, 103: 37}
    campaign = change(api, campaign, "remove", 103)
    assert shares(campaign) == {101: 25, 102: 75}


def test_last_active_offer_cannot_be_removed_and_failed_action_does_not_change_draft(api):
    campaign = create(api)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/draft",
        json={"revision": campaign["revision"], "action": "remove", "offer_id": 101},
    )
    assert response.status_code == 422
    assert api.get(f"/api/campaigns/{campaign['id']}").json() == campaign


def test_duplicate_addition_is_rejected_without_a_second_offer_row(api):
    campaign = create(api)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/draft",
        json={"revision": campaign["revision"], "action": "add", "offer_id": 101},
    )
    assert response.status_code == 422
    assert len(editable(api.get(f"/api/campaigns/{campaign['id']}").json())["offers"]) == 1


def test_stale_revision_cannot_overwrite_a_newer_draft(api):
    old = create(api)
    campaign = change(api, old, "add", 102)
    response = api.post(
        f"/api/campaigns/{old['id']}/streams/{editable(old)['id']}/draft",
        json={"revision": old["revision"], "action": "add", "offer_id": 103},
    )
    assert response.status_code == 409
    assert api.get(f"/api/campaigns/{old['id']}").json() == campaign


def test_fetch_requires_explicit_discard_for_an_unpublished_draft(api):
    campaign = change(api, create(api), "add", 102)
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 409
    assert api.get(f"/api/campaigns/{campaign['id']}").json() == campaign
    response = api.post(
        f"/api/campaigns/{campaign['id']}/fetch",
        json={"revision": campaign["revision"], "discard_draft": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["dirty"] is False
    assert shares(response.json()) == {101: 100}


def test_publish_detects_external_changes_and_preserves_the_draft(api):
    campaign = change(api, create(api), "add", 102)
    stream_id = editable(campaign)["id"]
    api.portal.call(
        api.app.state.service.client.update_offers,
        stream_id,
        [{"offer_id": 103, "share": 100, "state": "active"}],
    )
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{stream_id}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 409
    assert api.get(f"/api/campaigns/{campaign['id']}").json() == campaign
    assert [offer["offer_id"] for offer in remote_stream(api, campaign)["offers"]] == [103]


def test_google_stream_and_foreign_stream_ids_cannot_be_edited(api):
    campaign = create(api)
    google = next(stream for stream in campaign["streams"] if stream["read_only"])
    for stream_id, expected in [(google["id"], 422), (999_999, 404)]:
        response = api.post(
            f"/api/campaigns/{campaign['id']}/streams/{stream_id}/draft",
            json={"revision": campaign["revision"], "action": "add", "offer_id": 102},
        )
        assert response.status_code == expected


def test_foreign_campaigns_are_hidden_and_cannot_be_fetched(api):
    campaign = create(api)
    with api.app.state.sessions() as db:
        row = db.get(DemoCampaign, campaign["id"])
        data = deepcopy(row.data)
        data["campaign"]["name"] = "someone_else_campaign"
        row.data = data
        db.commit()
    assert api.get("/api/campaigns").json()["items"] == []
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 403


def test_public_settings_and_html_do_not_include_api_credentials(api):
    settings = api.get("/api/settings").json()
    assert set(settings) == {"mode", "campaign_prefix", "google_url"}
    html = api.get("/")
    assert html.status_code == 200
    assert "Api-Key" not in html.text
    assert "keitaro_api_key" not in html.text


def test_disabled_remote_offer_does_not_take_traffic_and_is_preserved_until_restored(api):
    campaign = create(api)
    stream_id = editable(campaign)["id"]
    api.portal.call(
        api.app.state.service.client.update_offers,
        stream_id,
        [
            {"offer_id": 101, "share": 100, "state": "active"},
            {"offer_id": 102, "share": 73, "state": "disabled"},
        ],
    )
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = response.json()
    assert shares(campaign) == {101: 100}
    disabled = next(offer for offer in editable(campaign)["offers"] if offer["offer_id"] == 102)
    assert disabled["state"] == "removed"
    assert disabled["share"] == 0
    campaign = publish(api, change(api, campaign, "add", 103))
    assert shares(campaign) == {101: 50, 103: 50}
    disabled_remote = next(
        offer for offer in remote_stream(api, campaign)["offers"] if offer["offer_id"] == 102
    )
    assert disabled_remote == {"offer_id": 102, "share": 73, "state": "disabled"}
    campaign = publish(api, change(api, campaign, "restore", 102))
    assert set(shares(campaign)) == {101, 102, 103}
    restored = next(offer for offer in remote_stream(api, campaign)["offers"] if offer["offer_id"] == 102)
    assert restored["state"] == "active"


def test_successful_put_that_did_not_apply_the_changes_is_not_marked_published(api, monkeypatch):
    campaign = change(api, create(api), "add", 102)
    original = remote_stream(api, campaign)

    async def ignored_write(stream_id, offers):
        return deepcopy(original)

    monkeypatch.setattr(api.app.state.service.client, "update_offers", ignored_write)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 409, response.text
    current = api.get(f"/api/campaigns/{campaign['id']}").json()
    assert current["dirty"] is True
    assert shares(current) == {101: 50, 102: 50}
    with api.app.state.sessions() as db:
        assert (
            db.scalars(select(Operation).where(Operation.kind == "publish")).one().status == "needs_attention"
        )


@pytest.mark.parametrize("payload", ["", "https://example.com/changed"])
def test_publication_distinguishes_blank_payload_normalization_from_meaningful_change(
    api, monkeypatch, payload
):
    campaign = change(api, create(api), "add", 102)
    write = api.app.state.service.client.update_offers

    async def server_normalizes_payload(stream_id, offers):
        result = await write(stream_id, offers)
        with api.app.state.sessions() as db:
            row = db.get(DemoCampaign, campaign["id"])
            data = deepcopy(row.data)
            target = next(s for s in data["streams"] if s["id"] == stream_id)
            target["action_payload"] = payload
            target["comments"] = ""
            row.data = data
            db.commit()
        return result

    monkeypatch.setattr(api.app.state.service.client, "update_offers", server_normalizes_payload)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == (200 if payload == "" else 409), response.text
    current = api.get(f"/api/campaigns/{campaign['id']}").json()
    assert current["dirty"] is bool(payload)


def test_import_displays_actual_invalid_total_without_turning_it_into_relative_weights(api):
    campaign = create(api)
    api.portal.call(
        api.app.state.service.client.update_offers,
        editable(campaign)["id"],
        [
            {"offer_id": 101, "share": 1, "state": "active"},
            {"offer_id": 102, "share": 1, "state": "active"},
        ],
    )
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = response.json()
    assert shares(campaign) == {101: 1, 102: 1}
    assert editable(campaign)["warning"]
    changed = change(api, campaign, "add", 103)
    assert shares(changed) == {101: 34, 102: 33, 103: 33}
    assert not editable(changed)["warning"]
    response = api.post(
        f"/api/campaigns/{changed['id']}/streams/{editable(changed)['id']}/cancel",
        json={"revision": changed["revision"]},
    )
    assert response.status_code == 200, response.text
    assert shares(response.json()) == {101: 1, 102: 1}
    assert editable(response.json())["warning"]


def test_pin_endpoint_rejects_a_fractional_percentage(api):
    campaign = change(api, create(api), "add", 102)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/draft",
        json={"revision": campaign["revision"], "action": "pin", "offer_id": 101, "share": "37.5"},
    )
    assert response.status_code == 422
    assert api.get(f"/api/campaigns/{campaign['id']}").json() == campaign


def test_publication_checks_exact_percentages_instead_of_accepting_equal_ratios(api, monkeypatch):
    campaign = change(api, create(api), "add", 102)
    write = api.app.state.service.client.update_offers

    async def alter_percentages(stream_id, offers):
        return await write(stream_id, [{**offer, "share": 1} for offer in offers])

    monkeypatch.setattr(api.app.state.service.client, "update_offers", alter_percentages)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 409, response.text
    assert api.get(f"/api/campaigns/{campaign['id']}").json()["dirty"] is True


def test_read_failure_after_a_successful_put_is_an_uncertain_operation(api, monkeypatch):
    campaign = change(api, create(api), "add", 102)
    upstream = api.app.state.service.client
    read = upstream.streams
    calls = 0

    async def fail_verification(campaign_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AppError(502, "Verification read failed")
        return await read(campaign_id)

    monkeypatch.setattr(upstream, "streams", fail_verification)
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{editable(campaign)['id']}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 502
    assert api.get(f"/api/campaigns/{campaign['id']}").json()["dirty"] is True
    with api.app.state.sessions() as db:
        assert (
            db.scalars(select(Operation).where(Operation.kind == "publish")).one().status == "needs_attention"
        )
    actual = api.portal.call(read, campaign["id"])
    assert {offer["offer_id"] for stream in actual for offer in stream["offers"]} == {101, 102}


def test_other_stream_changed_between_put_and_verification_keeps_its_conflict_baseline(api, monkeypatch):
    campaign = create(api)
    upstream = api.app.state.service.client
    second = api.portal.call(
        upstream.create_stream,
        {
            "campaign_id": campaign["id"],
            "name": "Another offer stream",
            "type": "regular",
            "schema": "landings",
            "position": 3,
            "state": "active",
            "offers": [{"offer_id": 103, "share": 100, "state": "active"}],
        },
    )
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = change(api, response.json(), "add", 102)
    first_id = editable(campaign)["id"]
    second_id = second["id"]
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{second_id}/draft",
        json={"revision": campaign["revision"], "action": "add", "offer_id": 104},
    )
    assert response.status_code == 200, response.text
    campaign = response.json()
    write = upstream.update_offers

    async def external_change_during_publish(stream_id, offers):
        result = await write(stream_id, offers)
        if stream_id == first_id:
            await write(second_id, [{"offer_id": 101, "share": 100, "state": "active"}])
        return result

    monkeypatch.setattr(upstream, "update_offers", external_change_during_publish)
    campaign = publish(api, campaign)
    first = next(stream for stream in campaign["streams"] if stream["id"] == first_id)
    second = next(stream for stream in campaign["streams"] if stream["id"] == second_id)
    assert first["dirty"] is False
    assert second["dirty"] is True
    response = api.post(
        f"/api/campaigns/{campaign['id']}/streams/{second_id}/publish",
        json={"revision": campaign["revision"]},
    )
    assert response.status_code == 409, response.text
    actual = api.portal.call(upstream.streams, campaign["id"])
    assert next(stream for stream in actual if stream["id"] == second_id)["offers"] == [
        {"offer_id": 101, "share": 100, "state": "active"},
    ]


def test_creation_failure_archives_the_incomplete_campaign(api, monkeypatch):
    upstream = api.app.state.service.client
    create_stream = upstream.create_stream
    calls = 0

    async def reject_second_stream(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AppError(502, "Keitaro rejected the second stream")
        return await create_stream(data)

    monkeypatch.setattr(upstream, "create_stream", reject_second_stream)
    response = api.post(
        "/api/campaigns",
        json={
            "name": "partial_failure",
            "country_code": "AU",
            "offer_id": 101,
            "domain_id": 1,
            "group_id": 1,
            "traffic_source_id": 1,
        },
    )
    assert response.status_code == 502
    assert api.get("/api/campaigns").json()["items"] == []
    with api.app.state.sessions() as db:
        operation = db.scalars(select(Operation).where(Operation.kind == "create")).one()
        assert operation.status == "rolled_back"
        assert operation.remote_id is not None


@pytest.mark.parametrize(
    "campaign,expected",
    [
        (
            {"url": "https://tracker.example.test/ready", "domain": "unused.example.test", "alias": "other"},
            "https://tracker.example.test/ready",
        ),
        ({"domain": "tracker.example.test", "alias": "abc123"}, "https://tracker.example.test/abc123"),
        (
            {"domain": "https://tracker.example.test/", "alias": "abc123"},
            "https://tracker.example.test/abc123",
        ),
        (
            {"domain": "http://tracker.example.test:8080", "alias": "abc123"},
            "http://tracker.example.test:8080/abc123",
        ),
        (
            {"domain": "tracker.example.test", "alias": "a/b ?#"},
            "https://tracker.example.test/a%2Fb%20%3F%23",
        ),
        (
            {"url": "javascript:alert(1)", "domain": "tracker.example.test", "alias": "safe"},
            "https://tracker.example.test/safe",
        ),
        ({"domain": "https://user:password@tracker.example.test", "alias": "abc"}, ""),
        ({"domain": "https://tracker.example.test/?token=private", "alias": "abc"}, ""),
        ({"domain": "https://tracker.example.test/a/path", "alias": "abc"}, ""),
        ({"domain": "https://tracker.example.test:invalid", "alias": "abc"}, ""),
        ({"domain": "tracker.example.test", "alias": ""}, ""),
        ({"domain_id": 1, "alias": "abc"}, ""),
    ],
)
def test_campaign_url_supports_domain_alias_without_source_parameters(campaign, expected):
    assert campaign_url(campaign) == expected


def test_publication_preserves_filters_landings_and_other_stream_configuration(api):
    campaign = create(api)
    stream_id = editable(campaign)["id"]
    with api.app.state.sessions() as db:
        row = db.get(DemoCampaign, campaign["id"])
        data = deepcopy(row.data)
        target = next(stream for stream in data["streams"] if stream["id"] == stream_id)
        target.update(
            filters=[{"name": "device_type", "mode": "accept", "payload": ["mobile"]}],
            landings=[{"landing_id": 987, "share": 100, "state": "active"}],
            action_options={"custom_option": "preserve this"},
            triggers=[{"id": 123, "enabled": True}],
            collect_clicks=False,
        )
        row.data = data
        db.commit()
    response = api.post(f"/api/campaigns/{campaign['id']}/fetch", json={"revision": campaign["revision"]})
    assert response.status_code == 200, response.text
    campaign = response.json()
    before = remote_stream(api, campaign)
    campaign = publish(api, change(api, campaign, "add", 102))
    after = remote_stream(api, campaign)
    assert {key: value for key, value in before.items() if key != "offers"} == {
        key: value for key, value in after.items() if key != "offers"
    }
