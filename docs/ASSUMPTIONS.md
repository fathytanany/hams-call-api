# Assumptions

The spec is intentionally incomplete, and Twilio's docs leave some behavior unstated. Each line names the gap and what we do about it.

## Accounts and API
1. **Accounts.** A single account. `ACCOUNT_SID` and `AUTH_TOKEN` come from the environment. If the path's `AccountSid` doesn't match the authenticated one we return 404 (20404), so we don't reveal that other accounts exist.
2. **Valid `To` numbers.** `To` must match `^\+?\d{1,15}$`, otherwise 400 (21211). It is used as a file name (`media/{To}.wav`), so this check is the path-traversal guard. `From` gets the same format check but is not checked against owned numbers.
3. **Call instructions.** Only `Twiml` is accepted. `Url` and `ApplicationSid` get a 400 saying they are unsupported. Missing `Twiml` gets 400 (21205).
4. **Unsupported TwiML.** It is rejected at create with a 400. Twilio would accept the call and fail at runtime; failing fast is more useful for an emulator. `Twiml` is limited to 4000 chars (Twilio's limit), which also bounds XML parsing cost.
5. **Stream URL scheme.** `ws://` is accepted as well as `wss://`, so the local test app runs without TLS.
6. **`Timeout`.** Values above 600 (Twilio's documented max) are clamped to 600. Values that aren't integers, or are below 1, get a 400.
7. **Update semantics.**
   - `Status=completed` on a `queued` or `ringing` call → `canceled`.
   - `Status=canceled` on an `in-progress` call → no-op, returns 200.
   - Any update to a call that has already ended → 200 with the call unchanged.
   - Ending a live call returns it as it is at that moment, usually still `in-progress`. The server running the call ends it at once if the request landed there, otherwise within ≤1s. A `hangup_requested` flag in Postgres carries the request to the owner.
   - Redirecting a call with `Url` / `Twiml` is not supported → 400.
8. **Paging.** `Page` and `PageSize` are offset-based, newest call first. `next_page_uri` carries `Page`, `PageSize` and the active filters, and clients follow it verbatim. A call created between page fetches shifts later pages by one; a real `PageToken` cursor would fix that (ADR-013 leaves it out). `PageSize` above 1000 is clamped to 1000.
9. **Fixed fields.** `direction` is `outbound-api`, `price` is `null`, `price_unit` is `USD`, and `to_formatted` / `from_formatted` echo the input.

## Call lifecycle
10. **Ringing and answer.** Ringing is instant except when a `.no-answer` marker exists. "Answer" means the WAV opened and passed validation (16-bit, mono, 8 or 16 kHz).
11. **Timestamps.**
    - `start_time` is when dialing started (the claim).
    - `queue_time` is `start_time − date_created` in ms.
    - `duration` runs from answer to end, rounded to the **nearest** whole second. Rounding up would report a 2s file as `"3"`, because connecting to the app and closing take a few ms.
    - `end_time` and `duration` are set only for `completed` calls, as Twilio documents.
12. **App connection.** Connecting to the app's WebSocket times out after 10s. A connect failure or timeout ends the call `completed` with the error logged, because it was already answered (ADR-010).
13. **How a call ends.**
    - The file ends → we send `stop`, close with code 1000, and the call is `completed`.
    - The app closes the socket → `completed`, no `stop` sent.
    - API hangup → we send `stop`, close, and the call is `completed`.
14. **Hangup latency.** An API hangup takes effect within ≤1s, the owner's poll interval (ADR-003).
15. **Per-server capacity.** Each server runs at most `MAX_CALLS` calls (default 500). Extra calls stay `queued`, and `queue_time` shows the wait. `MAX_CALLS=0` makes an API-only server, which the API tests use so their calls stay `queued`.

## Streams, audio and recording
16. **Message numbering.**
    - A single `sequenceNumber` counter covers every service→app message after `connected`, starting at 1.
    - `chunk` starts at 1.
    - `timestamp = (chunk − 1) × 20`.
    - Numbers are sent as strings.
17. **Bad messages from the app.** Unknown events, invalid JSON, or a wrong `streamSid` are ignored and logged; the call keeps going.
18. **App audio buffer.** App→service audio is capped at 60s of buffered playout. Beyond that the excess is dropped and logged. Twilio says any size is accepted; the cap bounds memory.
19. **Resampling.** `audioop.ratecv` has no anti-alias filter, so energy above 4 kHz in 16 kHz files can alias. That's acceptable for an emulator; production would use a polyphase filter.
20. **Recording content.**
    - Always on, regardless of the `Record` param, because the spec requires it.
    - Only the app's audio (one channel), silence-filled, starting at `start`.
    - Listed and downloadable once the call has ended.
    - One recording per call.
21. **Recording auth.** Downloads require Basic auth, like every other endpoint.

## Status callbacks
22. **Event values.** `StatusCallbackEvent` may be repeated or space-separated. Unknown values → 400.
23. **CallStatus mapping.** `initiated` sends `CallStatus=initiated`. `answered` sends `in-progress`.
24. **`SipResponseCode` on terminal events.** `completed` 200, `busy` 486, `no-answer` 487, `canceled` 487, `failed` 404.
25. **Queued cancel.** Canceling a `queued` call sends no callbacks (documented Twilio behavior).

## Multi-server
26. **Heartbeat timing.** Each server heartbeats every 1s and is considered dead after 10s without one. A false positive (a long GC pause or a network partition) is contained by owner fencing: the paused server's writes match 0 rows, so it stops those calls.
27. **Shared files.** All servers see the same `media/` and `recordings/` (a shared volume in compose). In production this would be object storage.
