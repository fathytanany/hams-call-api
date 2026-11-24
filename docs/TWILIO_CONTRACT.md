# Twilio contract we match

Extracted from Twilio's docs on 2026-11-22: the Call resource, Media Streams WebSocket messages, TwiML `<Stream>`, Twilio's response format, the Recording resource and webhook connection overrides. Anything the docs do not state but we have to decide is marked **(inferred)**. The reasoning behind those choices is in [ASSUMPTIONS.md](ASSUMPTIONS.md).

## 1. Calls API

Base: `/2010-04-01/Accounts/{AccountSid}`. Requests are form-encoded and responses are JSON. Auth is HTTP Basic with `AccountSid:AuthToken`.

| Operation | Method + path | Success |
|---|---|---|
| Create | `POST /Calls.json` | `201` + Call |
| Fetch | `GET /Calls/{CallSid}.json` | `200` + Call |
| List | `GET /Calls.json` | `200` + page envelope |
| Update (end) | `POST /Calls/{CallSid}.json` with `Status=completed` or `canceled` | `200` + Call **(inferred)** |

### Create parameters we accept
| Param | Notes |
|---|---|
| `To`, `From` | Required. A missing `To` gives 21201 and a missing `From` gives 21213. |
| `Twiml` | At most 4000 chars. Twilio accepts exactly one of `Url` / `Twiml` / `ApplicationSid`; we support only `Twiml`. |
| `StatusCallback` | Absolute URL. |
| `StatusCallbackEvent` | `initiated`, `ringing`, `answered`, `completed`. Repeat the param for several values (we also split on spaces). Default is `completed`. |
| `StatusCallbackMethod` | `GET` or `POST`, default `POST`. |
| `Timeout` | Seconds to ring before `no-answer`. Default `60`, max `600`. |

### List parameters
Filters: `To`, `From`, `Status`. Paging: `PageSize` (default 50, max 1000) and `Page` (zero-based). Clients follow `next_page_uri` verbatim, so the cursor format belongs to us.

Envelope keys: `calls`, `page`, `page_size`, `start`, `end`, `uri`, `first_page_uri`, `next_page_uri`, `previous_page_uri` (`null` when there is no such page).

### Update semantics
- `Status=completed` ends the call.
- `Status=canceled` applies to calls that are still `queued` or `ringing`.
- Canceling a call while it is `queued` fires **no** status callbacks (Twilio documents this).

### Call JSON (27 fields)
Dates are RFC 2822 in GMT, e.g. `Tue, 31 Aug 2010 20:36:28 +0000`. Twilio's examples use JSON `null` for absent values, and so do we.

| Field | Our value |
|---|---|
| `sid` | `CA` + 32 hex |
| `account_sid` | `AC…` from config |
| `parent_call_sid`, `phone_number_sid`, `group_sid`, `trunk_sid`, `forwarded_from`, `caller_name`, `answered_by`, `stir_verstat`, `price` | `null` |
| `to`, `from` | As given |
| `to_formatted`, `from_formatted` | Same as `to` / `from` **(inferred)** |
| `status` | See §2 |
| `date_created`, `date_updated` | RFC 2822 |
| `start_time` | When dialing started. `null` until then ("empty if not yet dialed"). |
| `end_time` | When the call ended. `null` unless the call completed successfully. |
| `duration` | **String** of whole seconds. `null` for busy, failed, no-answer, canceled and ongoing calls. |
| `queue_time` | **String**, milliseconds the call waited before being placed. |
| `price_unit` | `"USD"` |
| `direction` | `"outbound-api"` |
| `api_version` | `"2010-04-01"` |
| `uri` | `/2010-04-01/Accounts/{AC}/Calls/{CA}.json` |
| `subresource_uris` | An object with keys `notifications`, `recordings`, `payments`, `events`, `siprec`, `streams`, `transcriptions`, `user_defined_message_subscriptions`, `user_defined_messages`. Only `recordings` is served. |

## 2. Call status values

| Status | Twilio meaning | How the emulator gets there |
|---|---|---|
| `queued` | Created, waiting to dial | Row inserted, not yet claimed by a server |
| `ringing` | Ringing | Claimed; the WAV for `To` is being opened |
| `in-progress` | Answered | WAV opened; TwiML runs and the stream opens |
| `completed` | Answered, then ended normally | The file ended, the app closed the stream, or the call was ended via the API |
| `busy` | Busy signal | A `media/<To>.busy` marker exists |
| `no-answer` | No answer within `Timeout` | A `media/<To>.no-answer` marker exists (rings for `Timeout` seconds) |
| `failed` | Could not be completed as dialed | No WAV, or the WAV is unreadable or in the wrong format |
| `canceled` | Canceled via the API while queued or ringing | `Status=canceled` (or `completed`) before answer |

The emulator-side triggers are decided in [DECISIONS.md](DECISIONS.md) ADR-010.

## 3. Errors

Body: `{"code": <int>, "message": <str>, "more_info": <url>, "status": <http status>}`. `code` and `more_info` are only present when there is a Twilio error code.

| HTTP | Code | When |
|---|---|---|
| 400 | 21201 | `To` missing (Twilio's example message: "No to number is specified") |
| 400 | 21213 | `From` missing ("Caller phone number is required") |
| 400 | 21211 | `To` not a phone number **(inferred code)** |
| 400 | 21212 | `From` not a phone number **(inferred code)** |
| 400 | 21205 | No `Twiml` given **(inferred: Twilio's code for a missing/invalid Url)** |
| 400 | none | An unsupported parameter (`Url`, `ApplicationSid`) or unsupported TwiML. The message says so. |
| 401 | 20003 | Missing or invalid Basic credentials |
| 404 | 20404 | Unknown resource. Example message: "The requested resource was not found". |
| 405 | none | Method not allowed (e.g. `DELETE`) |

`more_info` is `https://www.twilio.com/docs/errors/{code}`.

## 4. TwiML subset

```xml
<Response>
  <Connect>
    <Stream url="wss://app.example/stream" name="optional">
      <Parameter name="mode" value="slow"/>
    </Stream>
  </Connect>
</Response>
```

- `<Connect><Stream>` makes a **bidirectional** stream. Twilio executes nothing after it until the app closes the socket. In our case nothing follows, so a closed socket means a hung-up call.
- `url`: Twilio supports only `wss`, and query strings are **not** supported. Per-call data goes in `<Parameter>`, which arrives in `start.customParameters`. Each parameter's name plus value must be under 500 chars.
- Under `<Connect>` only the `inbound_track` is available (the caller's audio sent to the app).

## 5. Media Streams WebSocket messages

Audio is `audio/x-mulaw`, 8000 Hz, 1 channel, base64-encoded, with no file headers. We send 20 ms chunks (160 bytes).

Numbers such as `sequenceNumber`, `chunk` and `timestamp` are **strings** in Twilio's examples. `sampleRate` and `channels` are numbers.

### Service → app

`connected` is the first message. It has no `sequenceNumber` and no `streamSid`:
```json
{"event": "connected", "protocol": "Call", "version": "1.0.0"}
```

`start` is sent once, right after `connected`:
```json
{"event": "start", "sequenceNumber": "1", "streamSid": "MZ…",
 "start": {"accountSid": "AC…", "streamSid": "MZ…", "callSid": "CA…",
           "tracks": ["inbound"], "customParameters": {"mode": "slow"},
           "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}
```

`media` carries one 20 ms chunk of the file. `chunk` starts at `"1"`. `timestamp` is milliseconds since the stream started:
```json
{"event": "media", "sequenceNumber": "2", "streamSid": "MZ…",
 "media": {"track": "inbound", "chunk": "1", "timestamp": "0", "payload": "<base64>"}}
```

`mark` echoes a mark the app sent, once all audio queued before it has played. A `clear` also returns marks:
```json
{"event": "mark", "sequenceNumber": "…", "streamSid": "MZ…", "mark": {"name": "greeting-done"}}
```

`stop` is sent when the call ends. It is not sent if the app closed the socket:
```json
{"event": "stop", "sequenceNumber": "…", "streamSid": "MZ…",
 "stop": {"accountSid": "AC…", "callSid": "CA…"}}
```

**(inferred)**
- `streamSid` is `MZ` + 32 hex.
- One `sequenceNumber` counter covers every service→app message after `connected`, starting at `"1"` with `start`.
- `timestamp = (chunk - 1) * 20`.
- Twilio's example shows `"5"` for chunk 1, which is network jitter we don't emulate.

### App → service (bidirectional only)

`media`: raw mulaw/8000 audio in base64, of **any size**. It is buffered and played in the order received:
```json
{"event": "media", "streamSid": "MZ…", "media": {"payload": "<base64>"}}
```

`mark`: echoed back when the audio buffered before it has finished playing, or straight away if nothing is buffered:
```json
{"event": "mark", "streamSid": "MZ…", "mark": {"name": "greeting-done"}}
```

`clear`: empties buffered audio, and every pending mark is sent back:
```json
{"event": "clear", "streamSid": "MZ…"}
```

## 6. Status callbacks

- Sent to `StatusCallback` using `StatusCallbackMethod`. POST bodies are form-encoded **(inferred: the docs' examples use form encoding)**.
- Events fire in order, but each is a separate request and **arrival order is not guaranteed**. Apps reorder with `SequenceNumber`.

| `StatusCallbackEvent` | `CallStatus` sent |
|---|---|
| `initiated` | `initiated` **(inferred: `initiated` is in Twilio's CallStatus enum for callbacks)** |
| `ringing` | `ringing` |
| `answered` | `in-progress` |
| `completed` | The terminal status: `completed`, `busy`, `failed`, `no-answer` or `canceled` |

Parameters:

| Param | Sent on |
|---|---|
| `CallSid`, `AccountSid`, `From`, `To`, `Caller` (= From), `Called` (= To), `CallStatus`, `ApiVersion`, `Direction` | Every event |
| `CallbackSource` = `call-progress-events` | Every event |
| `SequenceNumber` | Every event. Starts at `0` and counts callbacks fired for the call. |
| `Timestamp` | Every event **(inferred)**. RFC 2822. |
| `CallDuration` | Terminal events. Seconds. |
| `Duration` | `completed`. Minutes. |
| `SipResponseCode` | Terminal events. Twilio's examples: `404` (unreachable) and `487` (not connected before `Timeout`). |
| `RecordingUrl`, `RecordingSid`, `RecordingDuration` | `completed`. Twilio sends these only when `Record=true`; we always record, so we always send them. |

Not sent, because we have no data: `ForwardedFrom`, `CallerName`, `ParentCallSid`, the geo params, `StirStatus`.

### Webhook delivery defaults (connection overrides)
| Key | Meaning | Default |
|---|---|---|
| `ct` | Connect timeout | 5000 ms |
| `rt` | Read timeout | 15000 ms |
| `tt` | Total time including retries | 15000 ms |
| `rc` | Retry count | 1 |
| `rp` | Retry policy | `ct`: retry only on a TCP/TLS connect failure. 4xx and 5xx responses are not retried. |

### Signature: `X-Twilio-Signature`
1. Start with the full callback URL, including any query string.
2. For a POST, sort the parameter names (case-sensitive) and append each `name` + `value` with no delimiter.
3. Compute HMAC-SHA1 of that string, keyed with the AuthToken.
4. Base64-encode the result.

## 7. Recordings

| Operation | Path |
|---|---|
| List a call's recordings | `GET /Calls/{CallSid}/Recordings.json`. Envelope key `recordings`, plus the paging keys. |
| Fetch metadata | `GET /Recordings/{RecordingSid}.json` |
| Download media | `GET /Recordings/{RecordingSid}.wav`. No extension also returns WAV (`audio/x-wav`). |

Recording JSON fields: `sid` (`RE` + 32 hex), `account_sid`, `api_version`, `call_sid`, `conference_sid`, `date_created`, `date_updated`, `start_time`, `duration` (string, seconds), `price`, `price_unit`, `status`, `channels` (int), `source`, `error_code`, `uri`, `encryption_details`, `subresource_uris`, `media_url`.

- `status` enum: `in-progress`, `paused`, `stopped`, `processing`, `completed`, `absent`, `deleted`.
- `source` enum: `DialVerb`, `Conference`, `OutboundAPI`, `Trunking`, `RecordVerb`, `StartCallRecordingAPI`, `StartConferenceRecordingAPI`.

Our values:
- `status` is `completed` and `channels` is `1`.
- `source` is `OutboundAPI`, the source Twilio uses when a recording is requested on Calls-API create.
- `conference_sid`, `price`, `error_code` and `encryption_details` are `null`.
- `subresource_uris` is `{}` **(inferred)**.
- The media endpoint requires Basic auth **(inferred)**.
