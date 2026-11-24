# Build plan

Vertical slices of 30–60 minutes each. Every slice ends with `make check` green and a Conventional Commit (several small ones if the slice has distinct steps). After each slice, write 3–5 bullets on what changed and why.

- **Risky logic gets tests first:** pacing, state transitions, μ-law, failure paths.
- **Cut line:** if behind after slice 6, slice 8 is described in the design doc instead of built.
- **Spec time budget:** 3–4h. Anything not reached gets written up as "next", not rushed.

## 0. Decisions + scaffold (done in the first session)
- [x] `docs/TWILIO_CONTRACT.md`, `DECISIONS.md`, `ASSUMPTIONS.md`, `PLAN.md`
- [x] pyproject (ruff, mypy, pytest), Makefile, Dockerfile, compose stub, `media/+15550001.wav`

## 1. Audio core (45m)
`audio.py`:
- An iterator that turns a WAV into 20 ms μ-law frames: 8k/16k in, the last frame padded with `0xFF`, other formats rejected.
- A recording writer: μ-law in, 16-bit 8k WAV out.
- `patch_wav_header(path)` for files left half-written by a crash.

Checks:
- [x] Tests written first: μ-law known values (0 → `0xFF`), round-trip error within G.711 bounds
- [x] A 1s 16k file and a 1s 8k file each yield exactly 50 frames of 160 bytes
- [x] Stereo / 8-bit / 44.1k files raise a clear error
- [x] A truncated recording becomes readable by `wave` after `patch_wav_header`
- [x] `make check` green, committed

## 2. DB + Calls API (60m)
`schema.sql` (`calls`, `servers`), psycopg pool in the app lifespan, Basic auth, Twilio error bodies, create/fetch/list, TwiML parsing, Call JSON. Add a `db` service to compose; `make test` starts it.

Checks:
- [x] `curl -u AC…:token -d To=+15550001 -d From=+15550000 --data-urlencode Twiml=…` → 201 with all 27 Call fields and `status=queued`
- [x] Bad credentials → 401 / 20003; missing `To` → 400 / 21201; unknown sid → 404 / 20404
- [x] `To=../../etc/passwd` → 400 / 21211
- [x] List filters by `Status`, and following `next_page_uri` visits every call exactly once
- [x] `make check` green, committed

## 3. Call runner, happy path (60m)
- Supervisor: claims calls with `SKIP LOCKED` and gets a local poke when this server creates a call.
- Call task: ringing → in-progress → connect WS → `connected` / `start` / paced `media` / `stop` → completed.
- Fake WS app fixture for tests.

Checks:
- [x] Pacing test written first: 250 frames arrive in 5s ± 40ms with no cumulative drift; a forced 200 ms stall is caught up and never re-anchored
- [x] Message shapes match TWILIO_CONTRACT §5 (string numbers, `sequenceNumber` from 1, `chunk` from 1)
- [x] A 2s file → the app receives 100 media frames, then `stop`; the call is `completed` with `duration` "2"
- [x] `make check` green, committed

## 4. App audio, marks, recording (60m)
Receiver task, playout buffer, marks and `clear`, per-tick recording, the Recordings endpoints.

Checks:
- [x] Echo app → recording length equals call length, and contains the app's audio with silence where it was quiet
- [x] A mark sent after 1s of audio comes back about 1s later; a mark with nothing buffered comes back on the next tick
- [x] `clear` returns pending marks immediately and the cleared audio is not in the recording
- [x] `GET …/Calls/{CA}/Recordings.json` and `GET …/Recordings/{RE}.wav` return Twilio-shaped JSON and a valid WAV
- [x] `make check` green, committed

## 5. Endings and failure paths (60m)
Checks:
- [x] API `Status=completed` mid-call → `stop` sent, socket closed, `completed` within ≤1s
- [x] `Status=canceled` while queued → `canceled`; while in-progress → no-op
- [x] App closes the socket → `completed`, no `stop`
- [x] No WAV, or a bad WAV → `failed`; `.busy` → `busy`; `.no-answer` + `Timeout=2` → `no-answer` after about 2s
- [x] App refuses the connection, or never completes the handshake → `completed`, with the error logged
- [x] Slow app (reads at half speed) → drops counted, the pacer stays on schedule, the recording is unaffected
- [x] Dead app (stops reading, socket left open) → keepalive closes it → `completed`
- [x] `make check` green, committed

## 6. Status callbacks (45m)
Ordered per-call sender, event filter, params, `SequenceNumber`, signature, timeouts and retry, GET method.

Checks:
- [x] All four events subscribed → `initiated`, `ringing`, `in-progress`, `completed` arrive in order with `SequenceNumber` 0–3
- [x] Default (no events given) → only `completed`, with `CallDuration`, `Duration`, `SipResponseCode` 200 and the recording params
- [x] `X-Twilio-Signature` matches our HMAC computed per TWILIO_CONTRACT §6
- [x] A receiver that refuses the connection is retried once; a 500 response is not retried; a slow receiver doesn't delay the call
- [x] `make check` green, committed

## 7. Test app + demo (45m)
- `testapp.py`: saves what it hears to WAV and echoes audio back with marks. Behavior is chosen through `customParameters`: `mode=normal|slow|disconnect`, `disconnect_after`.
- `scripts/demo.py`: N concurrent calls with mixed modes and one mid-call hangup. Prints final statuses and recording sizes.
- Both added to compose.

Checks:
- [x] `docker compose up` → `make demo` runs 20 concurrent calls; every call ends in its expected status
- [x] 200 calls on one process: the maximum pacing lateness is reported (the number goes in the design doc)
  - `make demo CALLS=200` on Docker Desktop (10-core Mac, test app on the same host): 200/200 as expected, max lateness 29.4 ms, p95 27.4 ms, median 17.2 ms, no frames dropped. The calls are only 2s long, so this mostly measures 200 streams starting at once; no call drifted (each sent all 100 frames).
- [x] `make check` green, committed

## 8. Multi-server hardening (45m)
Heartbeat, reaper, owner fencing, a zombie server stopping its own calls, `api2` in compose, a shared recordings volume.

Checks:
- [x] Test runs two supervisors in one process; one dies without cleanup → after 10s the peer marks its calls `completed`, patches the WAV and sends the `completed` callback
- [x] A paused server that has been reaped finds its writes match 0 rows and closes its streams
- [x] Demo: `docker compose kill api1` mid-run → api1's calls end `completed`, and queued calls run on api2
  - `MAX_CALLS=10 docker compose up -d`, then `scripts/demo.py --api http://localhost:8001 --calls 40` and `docker compose kill api1` 1s in: 10 calls in progress on each server and 19 queued. api2 reaped api1's 10 calls exactly 10s after the kill and ran all 19 queued calls. 40/40 ended as expected, and the reaped calls' recordings download from api2.
- [x] `make check` green, committed

## 9. Design doc (45m)
- [ ] `docs/DESIGN.md`, at most 2 pages:
  - what's supported and how close it is to Twilio
  - real-time pacing and slow / disconnecting / never-answering apps
  - component failure and a server dying
  - what changes at 1,000 calls, and what's next
  - what we chose not to do
- [ ] README: run, test, demo commands
- [ ] Committed
