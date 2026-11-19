import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.errors import AppError
from app.keitaro import KeitaroClient

TEST_KEY = "unit-test-key-never-a-real-credential"


def run_client(handler, action):
    async def scenario():
        settings = Settings(
            _env_file=None,
            app_mode="live",
            keitaro_url="https://tracker.example.test/",
            keitaro_api_key=TEST_KEY,
        )
        client = KeitaroClient(settings, transport=httpx.MockTransport(handler))
        try:
            return await action(client)
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_api_key_is_a_server_side_header_and_not_a_query_parameter():
    def handler(request):
        assert request.url == "https://tracker.example.test/admin_api/v1/campaigns/7"
        assert request.headers["Api-Key"] == TEST_KEY
        assert request.headers["Accept"] == "application/json"
        assert TEST_KEY not in str(request.url)
        return httpx.Response(200, json={"id": 7, "name": "test_unit_campaign"})

    assert run_client(handler, lambda client: client.campaign(7))["id"] == 7


def test_update_offers_sends_a_partial_payload_and_no_unrelated_stream_fields():
    calls = []
    offers = [
        {"offer_id": 3, "share": 25, "state": "active"},
        {"offer_id": 4, "share": 75, "state": "active"},
    ]

    def handler(request):
        calls.append(request)
        assert request.method == "PUT"
        assert request.url.path == "/admin_api/v1/streams/12"
        assert json.loads(request.content) == {"offers": offers}
        return httpx.Response(200, json={"id": 12, "offers": offers})

    result = run_client(handler, lambda client: client.update_offers(12, offers))
    assert result["offers"] == offers
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 406, 429, 500, 503])
def test_upstream_error_bodies_and_credentials_are_not_exposed(status):
    def handler(request):
        return httpx.Response(status, text=f"SECRET {TEST_KEY} stack trace /private/internal.py")

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.request("GET", "offers"))
    assert error.value.status == 502
    assert TEST_KEY not in str(error.value)
    assert "stack trace" not in str(error.value)
    assert "/private/" not in str(error.value)
    assert error.value.uncertain is False


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_failed_mutations_are_not_retried_and_5xx_is_marked_uncertain(method):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": TEST_KEY})

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.request(method, "campaigns", json={"name": "test_unit_1"}))
    assert len(calls) == 1
    assert error.value.uncertain is True


@pytest.mark.parametrize(
    "method,uncertain", [("GET", False), ("POST", True), ("PUT", True), ("DELETE", True)]
)
def test_network_timeouts_do_not_repeat_requests(method, uncertain):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(f"private failure {TEST_KEY}", request=request)

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.request(method, "campaigns"))
    assert len(calls) == 1
    assert error.value.uncertain is uncertain
    assert TEST_KEY not in str(error.value)


@pytest.mark.parametrize("method,uncertain", [("GET", False), ("POST", True)])
def test_successful_response_with_invalid_json_is_a_safe_error(method, uncertain):
    def handler(request):
        return httpx.Response(200, text=f"<html>proxy error {TEST_KEY}</html>")

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.request(method, "campaigns"))
    assert error.value.status == 502
    assert error.value.uncertain is uncertain
    assert TEST_KEY not in str(error.value)


def test_redirects_are_not_followed_with_the_api_key():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://different.example.test/leak"})

    with pytest.raises(AppError):
        run_client(handler, lambda client: client.campaigns())
    assert calls == ["https://tracker.example.test/admin_api/v1/campaigns?offset=0&limit=100"]


def test_no_content_response_is_accepted_for_a_mutation():
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/admin_api/v1/campaigns/71"
        return httpx.Response(204)

    assert run_client(handler, lambda client: client.archive_campaign(71)) is None


def test_campaigns_are_paginated_until_the_last_partial_page():
    offsets = []

    def handler(request):
        assert request.url.params["limit"] == "100"
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        count = 5 if offset == 200 else 100
        return httpx.Response(200, json=[{"id": offset + i + 1} for i in range(count)])

    campaigns = run_client(handler, lambda client: client.campaigns())
    assert offsets == [0, 100, 200]
    assert len(campaigns) == 205
    assert campaigns[-1]["id"] == 205


def test_campaign_list_shape_is_validated():
    def handler(request):
        return httpx.Response(200, json={"items": []})

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.campaigns())
    assert error.value.status == 502


def test_catalog_uses_campaign_groups_and_combines_the_four_lists():
    requested = []

    def handler(request):
        resource = request.url.path.rsplit("/", 1)[-1]
        requested.append(resource)
        if resource == "groups":
            assert request.url.params["type"] == "campaigns"
        return httpx.Response(200, json=[{"id": 1, "name": resource}])

    result = run_client(handler, lambda client: client.catalog())
    assert set(requested) == {"domains", "groups", "traffic_sources", "offers"}
    assert set(result) == {"domains", "groups", "traffic_sources", "offers"}
    assert result["groups"] == [{"id": 1, "name": "groups"}]


def test_catalog_rejects_a_non_list_resource():
    def handler(request):
        payload = {} if request.url.path.endswith("/offers") else []
        return httpx.Response(200, json=payload)

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.catalog())
    assert error.value.status == 502


def test_known_rejected_mutation_is_not_marked_as_ambiguous():
    def handler(request):
        return httpx.Response(406, json={"error": "validation failed"})

    with pytest.raises(AppError) as error:
        run_client(handler, lambda client: client.create_campaign({"name": "test_unit_bad"}))
    assert error.value.uncertain is False
