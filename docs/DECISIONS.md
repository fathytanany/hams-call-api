# Decisions

Short ADRs, one per decision. Format: context → choice → why → rejected. Wire formats are in [TWILIO_CONTRACT.md](TWILIO_CONTRACT.md) and spec gaps are in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## ADR-001 Stack: Python 3.12, FastAPI, asyncio
- **Context:** The service is I/O-bound: 1,000 calls × 50 frames/s means ~50k small WebSocket messages/s across the fleet. It must be explained and changed live.
- **Choice:**
  - Python 3.12 managed with uv.
  - FastAPI + uvicorn[standard] (uvloop).
  - `websockets` for the client side and the test app.
  - httpx for callbacks.
- **Why:**
  - One asyncio task per call fits the problem: everything is waiting on sockets and timers.
  - It's the stack we can defend line by line.
  - The per-process ceiling gets measured (PLAN slice 7), and we scale by adding processes.
- **Rejected:**
  - Go: better raw throughput per core, but slower for us to change live.
  - Node: no advantage over asyncio here.

## ADR-002 State store: Postgres only, no Redis
- **Context:** Several servers share call state, and call records must survive restarts and be listable with filters.
- **Choice:** One Postgres database holding two tables, `calls` and `servers`.
- **Why:**
  - Call state must be durable and queryable, so Postgres is needed anyway.
  - Audio never touches the database. The load is about 5 status writes per call plus 1 heartbeat and 1 poll per server per second: tens of operations per second.
- **Rejected:**
  - SQLite: it can't be shared between servers, so "several servers" would exist only on paper.
  - Redis: it would only cut cross-server hangup latency from ≤1s to milliseconds (`LISTEN/NOTIFY` can do that too), at the cost of a dual write and a second failure mode. Worth adding for fleet-wide CPS limits or cross-server media routing, both out of scope.

## ADR-003 Call ownership: one task per call, claimed from a Postgres queue
- **Context:** A call must run on exactly one server, and API requests for it can land on any server.
- **Choice:**
  - `POST /Calls` inserts a `queued` row.
  - A server with free capacity claims it with `UPDATE … WHERE sid IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT free) RETURNING *`, which sets `owner`. The creating server pokes its own claimer so its calls start immediately; the others pick up overflow.
  - A per-server supervisor ticks every 1s. Each tick it writes the heartbeat row, reads the hangup flags for its own calls, and reaps dead servers.
  - Every write to a call includes `WHERE owner = me`. If it updates 0 rows, the server has lost ownership and stops the call.
- **Why:**
  - Load-aware placement and a real `queued` state come from a single query.
  - Hangup has one code path whichever server receives it.
  - Fencing stops a paused "zombie" server from overwriting a reaped call.
- **Rejected:**
  - A lease row per call: about 1,000 writes/s just for heartbeats.
  - The server that receives the create runs the call, with no queue: no capacity control, and an unstarted call dies with its server.
  - LISTEN/NOTIFY for hangup: instant, but it needs a dedicated connection and reconnect handling. Polling within ≤1s is enough.

## ADR-004 A server dies mid-call
- **Context:** The app's WebSocket lives in the dead process. Twilio never migrates a live media stream.
- **Choice:**
  - Peers treat a heartbeat older than 10s as dead and reap that server's calls:
    - `in-progress` → `completed`, with `end_time` = last heartbeat.
    - `ringing` → `failed`.
    - `queued` rows have no owner, so a peer simply claims them.
  - The reaper patches the partial WAV header so the recording can be downloaded, and sends the `completed` callback.
  - The `UPDATE … WHERE status IN (…) AND owner = dead` is atomic, so only one reaper wins.
- **Why:** It's Twilio-like: an answered call that drops ends as `completed`. No audio is invented and the app sees only one stream.
- **Rejected:**
  - Mark the call `failed`: Twilio reserves that for calls that never connected.
  - Re-dial on a peer and resume the file: the app would see a second `start` (not Twilio behavior), and it needs offset checkpoints.

## ADR-005 Real-time pacing on absolute deadlines
- **Context:** 20 ms frames must not drift over a 30-minute call.
- **Choice:**
  - Frame *n* is due at `t0 + n·20ms` on `time.monotonic()`; we sleep until that deadline.
  - If a tick is late, the next frames go out back-to-back until we are on time again. The schedule is never re-anchored.
  - Frames are read and converted one tick at a time, never preloaded.
- **Why:** Sleep error doesn't accumulate, and `timestamp` stays equal to wall-clock position. Preloading 30 minutes × 1,000 calls of μ-law would take ~14 GB.
- **Rejected:**
  - `sleep(0.02)` in a loop: each sleep's error adds up.
  - Re-anchoring after a stall: lost time is never recovered, so audio drifts.

## ADR-006 Backpressure for slow apps
- **Context:** An app can read slower than real time, or stop reading without closing.
- **Choice:**
  - The pacer pushes into a bounded deque of 50 messages (~1s), which a separate sender task drains.
  - When the deque is full, we drop the **oldest media** frame (never a `mark` or `stop`) and count the drop.
  - The `websockets` keepalive ping (20s) closes a dead app, which counts as the app closing the stream.
  - `close_timeout` is 2s, not websockets' 10s default. After a missed pong the close frame can't drain to an app that isn't reading, and the socket is only aborted once `close_timeout` passes, so the default would hold a dead call open 10s longer.
  - `sequenceNumber` is assigned when a message is sent, so dropped media leaves no gaps. The gap shows in `chunk`, which is the honest signal that audio was lost.
- **Why:**
  - The pacer, the marks and the recording never wait on the app.
  - Stale audio is worthless to a voice agent.
  - Memory stays bounded at 1,000 calls.
- **Rejected:**
  - Buffer about 10s, then hang up: adds up to 10s of latency and ~10s of audio held per call.
  - Await `send()` inside the pacer: one slow app would stall its own clock, marks and recording.

## ADR-007 Audio conversion with `audioop`
- **Context:** Input is a 16-bit PCM WAV at 8 or 16 kHz. Output is μ-law at 8 kHz. `audioop` was removed from the stdlib in 3.13.
- **Choice:**
  - stdlib `wave` for reading and writing.
  - `audioop.ratecv` for 16k→8k, carrying its state across frames.
  - `lin2ulaw` / `ulaw2lin` for μ-law.
  - Add `audioop-lts; python_version >= "3.13"` so the same import works on newer Pythons.
- **Why:** It's C-speed and battle-tested, and the spec explicitly allows libraries for WAV and μ-law.
- **Rejected:**
  - numpy: a heavy dependency for three calls.
  - Hand-written G.711: more code to defend, for no gain.

## ADR-008 Recording: played-out timeline, 16-bit WAV, Twilio Recordings resource
- **Context:** Twilio echoes a `mark` only after the audio before it has *played*, and `clear` drops buffered audio. The spec asks for a downloadable WAV per call.
- **Choice:**
  - App audio goes into a playout buffer drained at real time, one 160-byte frame per tick.
  - Each tick writes the played frame, or silence if the buffer is empty, to `recordings/{CallSid}.wav`: 16-bit PCM, 8 kHz, mono.
  - Marks fire when playout reaches them: on the tick that plays the last audio queued before the mark, so timing is accurate to one 20 ms frame. A mark sent with nothing buffered returns on the next tick. `clear` empties the buffer and returns all pending marks at once.
  - Files live on a volume shared by all servers.
  - Exposed through Twilio's Recording endpoints. The `RE` sid is derived from the call's hex, so there is no extra table.
- **Why:**
  - Marks behave as they do on Twilio.
  - The recording lines up with the call timeline.
  - Every player can open 16-bit WAV.
  - Unmodified Twilio clients can fetch the recording.
- **Rejected:**
  - Concatenating payloads as they arrive: marks would lie about playback and timing would be lost.
  - μ-law WAV: half the size, but stdlib `wave` can't write it.
  - Postgres `bytea` or S3: S3 isn't on the allowed list, and in production we'd use object storage instead of a shared volume.

## ADR-009 Status callbacks: ordered per call, in memory
- **Context:** Twilio doesn't guarantee arrival order (`SequenceNumber` lets apps reorder). Its default webhook policy is a 5s connect / 15s read timeout and 1 retry on connect failure.
- **Choice:**
  - Each call gets an in-memory queue and one sender task, so callbacks go out in order and never block the call.
  - Form POST, or GET when `StatusCallbackMethod` says so.
  - Signed with `X-Twilio-Signature`.
  - Twilio's default timeouts and retry policy.
  - A callback fires only after our own status UPDATE matched, so a call is never reported by two servers.
  - `SequenceNumber` is counted by the sender, not stored. Events fire in a fixed order, so the reaper can derive the `completed` number from the status it reaps (`ringing` or `in-progress`) and the subscribed events.
  - Senders outlive their call task, so a slow receiver never holds a call slot.
- **Why:**
  - Ordered delivery makes demos and tests deterministic.
  - Twilio apps often validate the signature and would reject unsigned requests.
- **Rejected:**
  - A Postgres outbox: durable, but it needs a table, a poller and retry bookkeeping.
  - Fire-and-forget: least code, but `ringing` could arrive after `in-progress`.
- **Known gap:** Queued callbacks are lost if the server dies or shuts down. The reaper still sends `completed`.
- **Revised in slice 6:** this ADR first stored the next `SequenceNumber` in a `callback_seq` column. It was dropped once the number proved derivable: one less piece of state to keep in step.

## ADR-010 What triggers each failure status
- **Context:** On Twilio the callee answers first, then the TwiML runs and the stream opens. Here the callee is the WAV file.
- **Choice:**
  - Missing, unreadable or wrong-format WAV → `failed`.
  - A `media/<To>.busy` marker file → `busy`.
  - A `media/<To>.no-answer` marker → the call rings for `Timeout` seconds, then `no-answer`.
  - API cancel while `queued` or `ringing` → `canceled`.
  - App WebSocket refused or timed out → `completed`: the call was answered, then the stream failed, as on Twilio.
- **Why:** It keeps Twilio's ordering, and app developers can test their busy and no-answer handling deterministically.
- **Rejected:**
  - Mapping app WebSocket failures to `busy` / `no-answer`: you could only test busy handling by breaking your own app.
  - WebSocket failure → `failed`: easier to spot, but a deviation from Twilio.

## ADR-011 Data access: psycopg 3 async, plain SQL
- **Choice:**
  - A psycopg 3 async pool, with `class_row` mapping rows to typed dataclasses.
  - `schema.sql` applied with `CREATE TABLE IF NOT EXISTS` at startup.
- **Why:** About ten queries, typed rows under mypy strict, and nothing hidden.
- **Rejected:**
  - SQLAlchemy/ORM: adds a layer for ten queries.
  - asyncpg: needs a separate stubs package for typing.
  - Alembic: we have one schema version.

## ADR-012 Tests
- **Choice:**
  - pytest + pytest-asyncio.
  - An in-process fake WebSocket app whose behavior can be set to normal, slow, disconnect or no answer.
  - Integration tests run against the compose Postgres.
  - Signatures are checked with our own HMAC.
  - No Twilio SDK anywhere, not even test-only.
- **Why:**
  - The risky logic (pacing, state transitions, μ-law, failure paths) is tested first.
  - Testing on the real database keeps `SKIP LOCKED` and fencing honest.
- **Rejected:** The twilio-python SDK as a test client: strong evidence of compatibility, but it conflicts with the project rule.

## ADR-013 Scope
- **In:**
  - Calls create, fetch, list and end.
  - `Twiml` with `<Connect><Stream>` and `<Parameter>`.
  - Media Streams: `connected`, `start`, `media`, `mark`, `stop` out; `media`, `mark`, `clear` in.
  - Status callbacks.
  - Recordings list, fetch and `.wav` download.
- **Out, and why:** The spec doesn't need these, and each would add surface without testing a decision.
  - `Url` / `ApplicationSid` (TwiML fetching).
  - Every other TwiML verb, and `<Start><Stream>`.
  - Stream `statusCallback`.
  - Outbound track to the app.
  - DTMF.
  - AMD.
  - The `Record` param and recording status callbacks (we always record).
  - Redirect, `TimeLimit` and fallback URLs.
  - `DELETE` call.
  - Subaccounts and API keys.
  - CSV/XML responses.
  - Real `PageToken` cursors.
  - CPS rate limiting.
  - Price.
  - `.mp3`.
