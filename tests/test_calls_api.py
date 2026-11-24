import io
import wave
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from callapi import config
from callapi.audio import RecordingWriter

BASE = f"/2010-04-01/Accounts/{config.ACCOUNT_SID}"
TWIML = (
    '<Response><Connect><Stream url="ws://localhost:9000/s">'
    '<Parameter name="mode" value="normal"/></Stream></Connect></Response>'
)
CALL_FIELDS = {
    "sid", "account_sid", "parent_call_sid", "phone_number_sid", "group_sid", "trunk_sid",
    "forwarded_from", "caller_name", "answered_by", "stir_verstat", "price", "to",
    "to_formatted", "from", "from_formatted", "status", "date_created", "date_updated",
    "start_time", "end_time", "duration", "queue_time", "price_unit", "direction",
    "api_version", "uri", "subresource_uris",
}  # fmt: skip


def create(client: TestClient, **overrides: Any) -> Any:
    data = {"To": "+15550001", "From": "+15550000", "Twiml": TWIML} | overrides
    return client.post(f"{BASE}/Calls.json", data={k: v for k, v in data.items() if v is not None})


def test_create_returns_queued_call_with_all_fields(client: TestClient) -> None:
    r = create(client)
    assert r.status_code == 201
    call = r.json()
    assert set(call) == CALL_FIELDS
    assert call["sid"].startswith("CA") and len(call["sid"]) == 34
    assert call["status"] == "queued"
    assert call["to"] == call["to_formatted"] == "+15550001"
    assert call["from"] == "+15550000"
    assert call["account_sid"] == config.ACCOUNT_SID
    assert call["direction"] == "outbound-api"
    assert call["uri"] == f"{BASE}/Calls/{call['sid']}.json"
    assert call["subresource_uris"]["recordings"] == f"{BASE}/Calls/{call['sid']}/Recordings.json"
    assert call["date_created"].endswith(" +0000")
    assert parsedate_to_datetime(call["date_created"]).tzinfo is not None
    assert call["start_time"] is call["duration"] is call["price"] is None


def test_fetch_returns_the_created_call(client: TestClient) -> None:
    created = create(client).json()
    r = client.get(f"{BASE}/Calls/{created['sid']}.json")
    assert r.status_code == 200
    assert r.json() == created


@pytest.mark.parametrize(
    ("auth", "path_account"),
    [(None, config.ACCOUNT_SID), ((config.ACCOUNT_SID, "wrong"), config.ACCOUNT_SID)],
)
def test_bad_credentials_get_401(
    client: TestClient, auth: tuple[str, str] | None, path_account: str
) -> None:
    client.auth = auth
    r = client.get(f"/2010-04-01/Accounts/{path_account}/Calls.json")
    assert r.status_code == 401
    assert r.json()["code"] == 20003
    assert r.headers["WWW-Authenticate"].startswith("Basic")


def test_malformed_basic_header_gets_401(client: TestClient) -> None:
    client.auth = None  # httpx would otherwise replace our header
    r = client.get(f"{BASE}/Calls.json", headers={"Authorization": "Basic !!notbase64"})
    assert (r.status_code, r.json()["code"]) == (401, 20003)


def test_other_account_is_not_found(client: TestClient) -> None:
    r = client.get(f"/2010-04-01/Accounts/AC{'f' * 32}/Calls.json")
    assert (r.status_code, r.json()["code"]) == (404, 20404)


def test_unknown_call_is_not_found(client: TestClient) -> None:
    r = client.get(f"{BASE}/Calls/CA{'0' * 32}.json")
    assert r.status_code == 404
    assert r.json() == {
        "code": 20404,
        "message": f"The requested resource {BASE}/Calls/CA{'0' * 32}.json was not found",
        "more_info": "https://www.twilio.com/docs/errors/20404",
        "status": 404,
    }


@pytest.mark.parametrize(
    ("overrides", "code", "fragment"),
    [
        ({"To": None}, 21201, "No 'To' number"),
        ({"From": None}, 21213, "Caller phone number"),
        ({"To": "../../etc/passwd"}, 21211, "not a valid phone number"),
        ({"To": "+1555/0001"}, 21211, "not a valid phone number"),
        ({"From": "evil"}, 21212, "not a valid phone number"),
        ({"Twiml": None}, 21205, "Twiml is required"),
        ({"Url": "https://x/twiml"}, None, "Url is not supported"),
        ({"Twiml": "<Response><Say>hi</Say></Response>"}, None, "<Connect>"),
        ({"StatusCallback": "ftp://x"}, None, "StatusCallback"),
        ({"StatusCallbackMethod": "PUT"}, None, "StatusCallbackMethod"),
        ({"StatusCallbackEvent": "answered bogus"}, None, "bogus"),
        ({"Timeout": "0"}, None, "Timeout"),
        ({"Timeout": "ten"}, None, "Timeout"),
    ],
)
def test_create_validation(
    client: TestClient, overrides: dict[str, str | None], code: int | None, fragment: str
) -> None:
    r = create(client, **overrides)
    assert r.status_code == 400
    body = r.json()
    assert body.get("code") == code
    assert fragment in body["message"]


def test_create_stores_callback_settings(client: TestClient) -> None:
    # Not in the Call JSON, so read the row the runner will use.
    sid = client.post(
        f"{BASE}/Calls.json",
        data={
            "To": "+15550001",
            "From": "+15550000",
            "Twiml": TWIML,
            "StatusCallback": "https://cb.example/status",
            "StatusCallbackMethod": "get",
            "StatusCallbackEvent": ["completed answered", "initiated"],
            "Timeout": "9999",
        },
    ).json()["sid"]
    with psycopg.connect(config.DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT stream_url, stream_params, status_callback, status_callback_method,"
            " status_callback_events, ring_timeout FROM calls WHERE sid = %s",
            (sid,),
        ).fetchone()
    assert row == (
        "ws://localhost:9000/s",
        {"mode": "normal"},
        "https://cb.example/status",
        "GET",
        ["initiated", "answered", "completed"],
        600,
    )


def test_list_filters_by_status_and_to(client: TestClient) -> None:
    a = create(client, To="+15550001").json()["sid"]
    create(client, To="+15550002")
    r = client.get(f"{BASE}/Calls.json", params={"Status": "queued", "To": "+15550001"})
    assert [c["sid"] for c in r.json()["calls"]] == [a]
    assert client.get(f"{BASE}/Calls.json", params={"Status": "completed"}).json()["calls"] == []


def test_next_page_uri_visits_every_call_once(client: TestClient) -> None:
    created = {create(client, To="+15550001").json()["sid"] for _ in range(7)}
    create(client, To="+15550009")  # filtered out; must not leak in via next_page_uri

    seen: list[str] = []
    uri: str | None = f"{BASE}/Calls.json?To=%2B15550001&PageSize=3"
    pages = []
    while uri:
        body = client.get(uri).json()
        pages.append(body)
        seen += [c["sid"] for c in body["calls"]]
        uri = body["next_page_uri"]

    assert len(seen) == len(set(seen)) and set(seen) == created
    assert [len(p["calls"]) for p in pages] == [3, 3, 1]
    assert [(p["page"], p["start"], p["end"]) for p in pages] == [(0, 0, 2), (1, 3, 5), (2, 6, 6)]
    assert pages[0]["previous_page_uri"] is None
    assert pages[1]["previous_page_uri"] == pages[0]["uri"]
    assert all(p["first_page_uri"] == pages[0]["uri"] for p in pages)


def test_list_is_newest_first(client: TestClient) -> None:
    sids = [create(client).json()["sid"] for _ in range(3)]
    assert [c["sid"] for c in client.get(f"{BASE}/Calls.json").json()["calls"]] == sids[::-1]


def test_unsupported_method_is_twilio_shaped(client: TestClient) -> None:
    r = client.delete(f"{BASE}/Calls.json")
    assert r.status_code == 405
    assert set(r.json()) == {"message", "status"}


RECORDING_FIELDS = {
    "sid", "account_sid", "api_version", "call_sid", "conference_sid", "date_created",
    "date_updated", "start_time", "duration", "price", "price_unit", "status", "channels",
    "source", "error_code", "uri", "encryption_details", "subresource_uris", "media_url",
}  # fmt: skip


def test_recording_is_listed_fetched_and_downloaded_once_the_call_ends(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path)
    sid = create(client).json()["sid"]
    re_sid = f"RE{sid[2:]}"
    with RecordingWriter(tmp_path / f"{sid}.wav") as w:
        for _ in range(100):  # 2s
            w.write(b"\x00" * 160)
    listing = f"{BASE}/Calls/{sid}/Recordings.json"

    # Still in progress: the file may be half-written, so it isn't listed yet.
    assert client.get(listing).json()["recordings"] == []
    assert client.get(f"{BASE}/Recordings/{re_sid}.wav").status_code == 404

    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE calls SET status = 'completed', start_time = now(), end_time = now()"
            " WHERE sid = %s",
            (sid,),
        )
    body = client.get(listing).json()
    [rec] = body["recordings"]
    assert set(rec) == RECORDING_FIELDS
    assert (body["page"], body["next_page_uri"]) == (0, None)
    assert rec["sid"] == re_sid and rec["call_sid"] == sid
    assert (rec["duration"], rec["status"], rec["channels"]) == ("2", "completed", 1)
    assert rec["source"] == "OutboundAPI"
    assert rec["uri"] == f"{BASE}/Recordings/{re_sid}.json"
    assert rec["media_url"] == f"http://testserver{BASE}/Recordings/{re_sid}"

    assert client.get(f"{BASE}/Recordings/{re_sid}.json").json() == rec
    for suffix in (".wav", ""):
        r = client.get(f"{BASE}/Recordings/{re_sid}{suffix}")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/x-wav"
        with wave.open(io.BytesIO(r.content)) as w:
            assert (w.getframerate(), w.getsampwidth(), w.getnframes()) == (8000, 2, 16000)

    client.auth = None
    assert client.get(f"{BASE}/Recordings/{re_sid}.wav").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        f"/Calls/CA{'0' * 32}/Recordings.json",
        f"/Recordings/RE{'0' * 32}.json",
        f"/Recordings/RE{'0' * 32}",
        "/Recordings/RE..%2F..%2Fetc%2Fpasswd.wav",
        "/Recordings/..%2F..%2Fpyproject.toml",
    ],
)
def test_unknown_recordings_are_not_found(client: TestClient, path: str) -> None:
    r = client.get(BASE + path)
    assert (r.status_code, r.json()["code"]) == (404, 20404)


def set_status(sid: str, status: str) -> None:
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE calls SET status = %s WHERE sid = %s", (status, sid))


def hangup_requested(sid: str) -> bool:
    with psycopg.connect(config.DATABASE_URL) as conn:
        row = conn.execute("SELECT hangup_requested FROM calls WHERE sid = %s", (sid,)).fetchone()
    assert row is not None
    return bool(row[0])


@pytest.mark.parametrize("status", ["canceled", "completed"])
def test_ending_a_queued_call_cancels_it(client: TestClient, status: str) -> None:
    sid = create(client).json()["sid"]
    r = client.post(f"{BASE}/Calls/{sid}.json", data={"Status": status})
    assert (r.status_code, r.json()["status"]) == (200, "canceled")
    assert r.json()["end_time"] is r.json()["duration"] is None


@pytest.mark.parametrize(
    ("current", "requested", "flagged"),
    [
        ("in-progress", "completed", True),  # the owner hangs up within 1s
        ("in-progress", "canceled", False),  # canceled only applies before answer: no-op
        ("ringing", "canceled", True),
        ("completed", "completed", False),  # already ended: unchanged
        ("busy", "canceled", False),
    ],
)
def test_ending_a_live_or_ended_call(
    client: TestClient, current: str, requested: str, flagged: bool
) -> None:
    sid = create(client).json()["sid"]
    set_status(sid, current)
    r = client.post(f"{BASE}/Calls/{sid}.json", data={"Status": requested})
    assert (r.status_code, r.json()["status"]) == (200, current)
    assert hangup_requested(sid) is flagged


@pytest.mark.parametrize("data", [{}, {"Status": "in-progress"}, {"Twiml": "<Response/>"}])
def test_update_only_ends_calls(client: TestClient, data: dict[str, str]) -> None:
    sid = create(client).json()["sid"]
    r = client.post(f"{BASE}/Calls/{sid}.json", data=data)
    assert r.status_code == 400
    assert "only ending a call is supported" in r.json()["message"]


def test_updating_an_unknown_call_is_not_found(client: TestClient) -> None:
    r = client.post(f"{BASE}/Calls/CA{'0' * 32}.json", data={"Status": "completed"})
    assert (r.status_code, r.json()["code"]) == (404, 20404)
