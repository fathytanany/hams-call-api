# Twilio-compatible Call API

Places outbound "calls" whose callee is a WAV file in `media/{To}.wav` and streams them to your app over Twilio's Media Streams WebSocket protocol, with Twilio-shaped Calls, Recordings and status callbacks. Design: [docs/DESIGN.md](docs/DESIGN.md).

## Run
Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up --build
```

This starts Postgres, two API servers (`:8000`, `:8001`) and a test app. To run one server locally with reload instead:

```bash
make run
```

Place a call (dev credentials from `src/callapi/config.py`; override with `ACCOUNT_SID` / `AUTH_TOKEN`):

```bash
curl -u AC00000000000000000000000000000001:dev-auth-token \
  http://localhost:8000/2010-04-01/Accounts/AC00000000000000000000000000000001/Calls.json \
  -d To=+15550001 -d From=+15550000 \
  --data-urlencode 'Twiml=<Response><Connect><Stream url="ws://testapp:8765/stream"/></Connect></Response>'
```

Recordings land in the shared `recordings` volume and download from `…/Recordings/{RE…}.wav`.

## Test
```bash
make check
```

Runs ruff, mypy (strict) and pytest; it starts the compose Postgres first.

## Demo
With `docker compose up` running:

```bash
make demo CALLS=20
```

Places concurrent calls in mixed modes (normal, slow, disconnecting, one API hangup, one missing WAV), then prints each call's final status, duration and recording size. Exits 1 if any call ends unexpectedly. Each call's max pacing lateness is in the API logs (`docker compose logs api1 | grep lateness`).

Failover: start with `MAX_CALLS=10 docker compose up -d`, run `uv run python scripts/demo.py --api http://localhost:8001 --calls 40`, and `docker compose kill api1` a second in. api2 reaps api1's calls after 10s and runs the queued ones.
