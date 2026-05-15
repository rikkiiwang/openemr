"""Unit tests for GET /v1/patients.

The endpoint exists so external tooling (the AgentForge Adversarial
harness) can auto-bootstrap a patient_id without operator copy-paste.

We exercise the endpoint coroutine directly (seeding ``app.state.fhir``
manually) — the lifespan startup is heavy (corpus build, Anthropic
client) and not relevant to what we're testing. Matches the pattern
established by ``test_panel_scope.py``.
"""
from __future__ import annotations

import json

import pytest
import respx
from fastapi import HTTPException
from httpx import Response

from app.config import get_settings
from app.fhir.client import FhirClient
from app.main import app, list_patients


PATIENT_A = "11111111-1111-4111-8111-111111111111"
PATIENT_B = "22222222-2222-4222-8222-222222222222"
PATIENT_C = "33333333-3333-4333-8333-333333333333"


def _bundle(*ids: str) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": pid}} for pid in ids
        ],
    }


def _token_response() -> dict:
    return {"access_token": "tok-test", "expires_in": 300, "id_token": "h.e.s"}


@pytest.fixture
def fhir():
    """Fresh FhirClient seeded into app.state for the endpoint to find."""
    client = FhirClient(get_settings())
    app.state.fhir = client
    yield client


@pytest.fixture(autouse=True)
def _reset_panel_env(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "physician_patient_panel", "{}")
    yield


@respx.mock
async def test_list_patients_returns_ids_from_bundle(fhir):
    settings = get_settings()
    respx.post(settings.openemr_oauth_base + "/token").mock(
        return_value=Response(200, json=_token_response())
    )
    respx.get(f"{settings.openemr_fhir_base}/Patient").mock(
        return_value=Response(200, json=_bundle(PATIENT_A, PATIENT_B))
    )

    body = await list_patients(physician_user_id=None, limit=5, settings=settings)

    assert body["count"] == 2
    assert [p["id"] for p in body["patients"]] == [PATIENT_A, PATIENT_B]


@respx.mock
async def test_list_patients_filters_by_panel(fhir, monkeypatch):
    """PHYSICIAN_PATIENT_PANEL intersection — out-of-panel UUIDs dropped."""
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "physician_patient_panel",
        json.dumps({"dr_alvarez": [PATIENT_A, PATIENT_C]}),
    )
    respx.post(settings.openemr_oauth_base + "/token").mock(
        return_value=Response(200, json=_token_response())
    )
    respx.get(f"{settings.openemr_fhir_base}/Patient").mock(
        return_value=Response(200, json=_bundle(PATIENT_A, PATIENT_B, PATIENT_C))
    )

    body = await list_patients(
        physician_user_id="dr_alvarez", limit=10, settings=settings
    )

    assert body["count"] == 2
    assert [p["id"] for p in body["patients"]] == [PATIENT_A, PATIENT_C]


@respx.mock
async def test_list_patients_propagates_fhir_401(fhir):
    """FHIR access denied → HTTP 401 (FhirError preserves the status)."""
    settings = get_settings()
    respx.post(settings.openemr_oauth_base + "/token").mock(
        return_value=Response(200, json=_token_response())
    )
    respx.get(f"{settings.openemr_fhir_base}/Patient").mock(
        return_value=Response(401, json={"error": "unauthorized"})
    )

    with pytest.raises(HTTPException) as ei:
        await list_patients(physician_user_id=None, limit=5, settings=settings)
    assert ei.value.status_code == 401


@respx.mock
async def test_list_patients_bounded_limit(fhir):
    """``limit`` query param is clamped to [1, 100] before reaching FHIR."""
    settings = get_settings()
    captured: list[str] = []

    def _capture(request):
        captured.append(request.url.params.get("_count", ""))
        return Response(200, json=_bundle())

    respx.post(settings.openemr_oauth_base + "/token").mock(
        return_value=Response(200, json=_token_response())
    )
    respx.get(f"{settings.openemr_fhir_base}/Patient").mock(side_effect=_capture)

    await list_patients(physician_user_id=None, limit=0, settings=settings)
    await list_patients(physician_user_id=None, limit=9999, settings=settings)

    assert captured == ["1", "100"]


@respx.mock
async def test_list_patients_returns_empty_when_panel_blocks_all(fhir, monkeypatch):
    """Panel set, but no FHIR-returned UUID matches → empty list, not 500."""
    settings = get_settings()
    monkeypatch.setattr(
        settings,
        "physician_patient_panel",
        json.dumps({"dr_alvarez": ["unrelated-uuid"]}),
    )
    respx.post(settings.openemr_oauth_base + "/token").mock(
        return_value=Response(200, json=_token_response())
    )
    respx.get(f"{settings.openemr_fhir_base}/Patient").mock(
        return_value=Response(200, json=_bundle(PATIENT_A, PATIENT_B))
    )

    body = await list_patients(
        physician_user_id="dr_alvarez", limit=5, settings=settings
    )

    assert body == {"patients": [], "count": 0}
