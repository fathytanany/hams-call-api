# Design

A Twilio-compatible outbound call service. A Twilio client points at it, places a call with `<Connect><Stream>` TwiML, and its WebSocket app talks to a "callee" that is a WAV file. The reasoning behind each choice is in [DECISIONS.md](DECISIONS.md) (ADR-n), spec gaps in [ASSUMPTIONS.md](ASSUMPTIONS.md), wire formats in [TWILIO_CONTRACT.md](TWILIO_CONTRACT.md).

```
client ──HTTP──▶ api1 ─┐                ┌─ call task ──WS──▶ app
client ──HTTP──▶ api2 ─┼── Postgres ────┤  (pacer, receiver, playout, recording)
                       │ calls, servers │
                       └── recordings/ (shared volume) ◀── 16-bit WAV per call
```

## What's supported, and how close it is to Twilio
- **Calls API:** create, fetch, list (with `Status`/`To`/`From` filters and `next_page_uri` paging), and end via `Status=completed|canceled`. All 27 Call fields, Basic auth, Twilio error bodies and codes (20003, 20404, 21201, 21205, 21211).
- **TwiML:** `<Response><Connect><Stream url>` with `<Parameter>`s, delivered to the app as `customParameters`. Anything else is a 400 at create (Twilio would fail at runtime; failing fast is kinder in an emulator).
- **Media Streams:** `connected`, `start`, paced `media`, `mark`, `stop` out; `media`, `mark`, `clear` in. String numbers, `sequenceNumber`/`chunk` from 1, `timestamp` = wall-clock position. Marks echo when playout *reaches* them, as on Twilio.
- **Statuses:** `queued → ringing → in-progress → completed`, plus `busy`, `no-answer`, `failed`, `canceled`. The callee is a file, so `media/{To}.busy` and `{To}.no-answer` markers let app developers test those paths deterministically (ADR-010).
- **Status callbacks:** event filter, ordered delivery, `SequenceNumber`, `X-Twilio-Signature`, POST or GET, Twilio's default timeouts and one retry on connect failure.
- **Recordings:** always on; Twilio-shaped Recording JSON and a `.wav` download. The recording is the app's audio on the played-out timeline, silence-filled.
- **Known deviations:** `ws://` accepted; one account; offset paging rather than `PageToken`; recording is always on and app-side only. All listed in ASSUMPTIONS.

## Real-time pacing and badly behaved apps
- **Pacing (ADR-005):** frame *n* is due at `t0 + n·20ms` on the monotonic clock. A late tick is caught up back-to-back and the schedule is never re-anchored, so a 30-minute call doesn't drift. Frames are read and converted one tick at a time; nothing is preloaded.
- **Slow app (ADR-006):** the pacer never awaits the socket. It pushes into a bounded ~1s outbox drained by a sender task; when full, the oldest *media* frame is dropped (never a `mark` or `stop`) and counted. The gap shows in `chunk`. The pacer, marks and recording stay on schedule.
- **Dead app:** stops reading with the socket open → the 20s keepalive ping fails → closed after a 2s `close_timeout` → `completed`.
- **Disconnecting app:** closes the socket → `completed`, no `stop` sent.
- **Never-answering app:** connect refused or no handshake in 10s → `completed` with the error logged. The callee had already answered, so this matches Twilio (ADR-010).
- **App flooding audio:** playout buffer capped at 60s; excess dropped and logged.
- **Measured:** 200 concurrent calls in one process on a laptop: max pacing lateness 29.4 ms, p95 27.4 ms, no frames dropped (PLAN slice 7).

## Component failure and a server dying
- **Ownership (ADR-003):** a call row is claimed by one server with `FOR UPDATE SKIP LOCKED`, up to `MAX_CALLS`. Every later write carries `WHERE owner = me`. A hangup on any server sets a flag the owner polls each second.
- **Server dies (ADR-004):** peers see its heartbeat go 10s stale. One statement deletes its `servers` row and updates its calls, so exactly one peer reaps. `in-progress` → `completed` with `end_time` = last heartbeat; `ringing` → `failed`; `queued` calls had no owner and are simply claimed by a peer. The reaper patches the half-written WAV header and sends the `completed` callback with the right `SequenceNumber`. The live stream is not migrated; Twilio doesn't migrate one either.
- **Zombie server:** a paused server that was reaped finds its heartbeat update matches 0 rows, stops its streams (`stop` to the app), and its fenced writes can't overwrite the reaper's record.
- **Demonstrated:** `docker compose kill api1` with 10 calls live on each server and 19 queued: api2 reaped api1's calls 10s later, ran all queued calls, 40/40 ended as expected.
- **Postgres down:** API requests fail with 5xx and the supervisor logs and retries each tick. Running calls keep streaming (the media path never touches the database), and each call's status writes retry every second until it is back, so no call is left `in-progress` by a live owner. `duration` is measured on the server's monotonic clock, so the outage isn't billed as call time. If the outage outlasts 10s, peers may reap the call when the database returns; fencing then makes the owner's retried write a no-op. One database is still the single point of failure (managed HA in production).
- **Callback receiver down:** retried once on connect failure, never blocks a call. Queued callbacks die with their server (ADR-009 known gap); the reaper still sends `completed`.

## What changes at 1,000 calls
- **Per-process ceiling:** ~50k WebSocket messages/s each way at 1,000 calls. One Python process handled 200 cleanly; plan for 2–4 processes (`MAX_CALLS` ≈ 300) and keep lateness as the alert metric. More servers is a compose/k8s replica count, nothing else.
- **Postgres is not the bottleneck:** ~5 writes per call lifetime plus one heartbeat, one claim and one hangup poll per server per second. Audio never touches it.
- **Recordings become the I/O hotspot:** 1,000 × 16 KB/s ≈ 16 MB/s of small writes to a shared volume. Next step: write locally and upload to object storage on end, with the reaper uploading for dead servers.
- **Callbacks:** an in-memory outbox per call is fine at this rate; a Postgres outbox would make them survive restarts.

## What's next
- Graceful drain on SIGTERM (stop claiming, let calls finish) instead of leaving them to the reaper.
- `LISTEN/NOTIFY` to cut cross-server hangup latency from ≤1s to milliseconds.
- Durable callback outbox; object storage for recordings; a polyphase resampler for 16k input.
- `PageToken` cursors; CPS limits; `Url` TwiML fetching.

## What we chose not to do
- **Redis** (ADR-002): Postgres already holds durable state; Redis would only add a dual write and a second failure mode.
- **Migrating live calls** to a peer (ADR-004): the app would see a second `start`, which Twilio never sends.
- **Buffering a slow app for seconds** (ADR-006): adds latency a voice agent can't use, and memory at scale.
- **ORM / migrations** (ADR-011): ten queries, one schema version.
- **Twilio SDK, emulators or media servers** (ADR-012): call handling, streaming and recording are our own code; signatures are checked with our own HMAC.
- **The long tail of Twilio** (ADR-013): other TwiML verbs, DTMF, AMD, `<Start><Stream>`, stream status callbacks, pricing, `.mp3`. None tests a decision the spec asks about.
