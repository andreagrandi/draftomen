# MTG Arena draft log format

This document records the Arena log tokens Draft Omen reads for Quick, Premier, Traditional and Pick-Two drafts. The Quick Draft sections quote the sanitized fixture at `tests/fixtures/quick-draft-msh-player.log`. The human-draft sections describe the protocol at field level only. Real human-draft logs are private, so this document quotes none of them. See [Private logs](#private-logs).

## Format detection

Draft Omen decides the format from event metadata before it reads any pack. It never guesses the format from pack size or card count. The event name comes from the first of these that appears:

- An `EventJoin` request whose `EventName` starts with a known prefix.
- A course payload whose `CurrentModule` is `PlayerDraft` and whose `InternalEventName` holds the event name.
- The scene line `Entering table draft queue: <SET>_<Kind>_Draft`, for logs that have no `EventJoin` line. `Premier`, `Trad` and `PickTwo` map to the `PremierDraft_`, `TradDraft_` and `PickTwoDraft_` prefixes. An `EventJoin` for the same set and format that follows keeps its dated event name.

The event name prefix picks the format and its pack rules:

| Prefix | Format | Packs | Logical picks per pack | Cards per pick | Pool |
|---|---|---|---|---|---|
| `QuickDraft_` | Quick | 3 | 14 | 1 | 42 |
| `PremierDraft_` | Premier | 3 | 14 | 1 | 42 |
| `TradDraft_` | Traditional | 3 | 14 | 1 | 42 |
| `PickTwoDraft_`, `PickTwoTradDraft_` | Pick-Two | 3 | 7 | 2 | 42 |

The longest matching prefix wins. An unknown prefix leaves the active draft alone. Traditional and Premier differ in match rules, not in how packs are picked, so they share one drafting path. `PickTwoTradDraft_` appears in 17Lands format lists but in no log we hold.

A logical pick is one submission to Arena. It holds `cards_per_pick` cards, so a Pick-Two draft has 21 logical picks and 42 cards. Draft Omen counts progress in logical picks and pool size in cards.

## Line prefixes

Arena's rotated `UTC_Log` files start each line with a `[<thread>] ` prefix. The parser strips that prefix and then reads the line like a plain `Player.log` line. Tags such as `[Accounts - Login]` and `[UnityCrossThreadLogger]` are kept.

## Fixture

- Source: one complete Quick Draft on macOS with Detailed Logs (Plugin Support) enabled.
- Event: `QuickDraft_MSH_20260702`.
- Set code: `MSH`, parsed from the event name segment after `QuickDraft_`.
- Draft shape: 3 packs, 14 picks per pack, 42 total picks.
- Numbering: `PackNumber` and `PickNumber` are zero-indexed in the log.

The fixture keeps the draft protocol payloads and all card `grpId` values intact. Personal account identifiers, request IDs, session IDs, course IDs, local paths, and inventory/cosmetic data were pseudonymized or removed because parsing does not need them.

## Account identity

Arena logs account identity near session start and match authentication. The most useful parser token is `authenticateResponse`:

```log
{ "transactionId": "00000000-0000-4000-8000-000000000001", "requestId": 1, "timestamp": "639186578104553274", "authenticateResponse": { "clientId": "FIXTURECLIENTID1234567890", "sessionId": "00000000-0000-4000-8000-000000000002", "screenName":"FixturePlayer" } }
```

Use `authenticateResponse.clientId` as the MTGA account key for current logs and `screenName` for display. `sessionId` is session-scoped and should not be used as the account key. Cross-patch stability still needs future revalidation; if `clientId` disappears, fall back to `screenName` plus a manual `--account` override. When `authenticateResponse.screenName` is missing or only repeats the client id, the parser uses the nearby login UI line as a display-name fallback:

```log
[Accounts - Login] Logged in successfully. Display Name: FixturePlayer#12345
```

Draft Omen stores the latest verified display name in a separate per-account
profile in its app data directory. That profile labels every recovered draft for
that account, including legacy snapshots written before display-name support.
When Arena emits a login line without its matching authentication response, the
TUI first tries to uniquely match that display name, with or without its numeric
`#` discriminator, to a saved account profile. It can otherwise associate the
login only with recovered drafts whose Quick Draft course id is present in the
same session's course snapshot; if neither method is unambiguous, it leaves the
account unresolved rather than guessing.

## Quick Draft start

A paid Quick Draft entry appears as an `EventJoin` request followed by a course payload whose `InternalEventName` is the event id and whose `CurrentModule` is `BotDraft`:

```log
[UnityCrossThreadLogger]==> EventJoin {"id":"00000000-0000-4000-8000-000000000003","request":"{\"EventName\":\"QuickDraft_MSH_20260702\",\"EntryCurrencyType\":\"Gold\",\"EntryCurrencyPaid\":5000,\"CustomTokenId\":null,\"EventChoice\":\"\",\"DebugIgnoreEntryLimits\":false}"}
{"Course":{"CourseId":"00000000-0000-4000-8000-000000000004","InternalEventName":"QuickDraft_MSH_20260702","CurrentModule":"BotDraft","ModulePayload":"","CourseDeckSummary":{"Attributes":[]},"CardPool":[],"CardStyles":[]},"InventoryInfo":{"SeqId":23,"Changes":[]}}
```

Persist draft state by `(account clientId, CourseId/InternalEventName)`. The event id gives the set code and the course id disambiguates a concrete run of that event.

Arena can also emit Quick Draft course snapshots after the course has moved to another module, such as `DeckSelect`.
Those snapshots are not draft-start events and should be ignored; pack presentation and completion are parsed from the module payload lines below.

## Pack presentation

The initial P1P1 pack is requested with `BotDraftDraftStatus`; the response body has `CurrentModule: "BotDraft"` and a string-encoded JSON `Payload`:

```log
[UnityCrossThreadLogger]==> BotDraftDraftStatus {"id":"00000000-0000-4000-8000-000000000005","request":"{\"EventName\":\"QuickDraft_MSH_20260702\"}"}
<== BotDraftDraftStatus(00000000-0000-4000-8000-000000000005)
{"CurrentModule":"BotDraft","Payload":"{\"Result\":\"Success\",\"EventName\":\"QuickDraft_MSH_20260702\",\"DraftStatus\":\"PickNext\",\"PackNumber\":0,\"PickNumber\":0,\"NumCardsToPick\":1,\"DraftPack\":[\"104894\",\"104976\",\"105080\",\"104995\",\"105027\",\"105030\",\"105170\",\"104932\",\"104893\",\"105091\",\"104969\",\"105097\",\"104979\",\"105164\"],\"PackStyles\":[],\"PickedCards\":[],\"PickedStyles\":[]}"}
```

Parse `Payload.DraftPack` as the offered card `grpId` list. The IDs are strings in this fixture and should be normalized to integers by the parser. Draft state retains each offered pack and its pool snapshot. When the latest offer has no chosen card yet, cycling back to that account reconstructs and rescores the pending pack instead of showing an empty recovered-draft view.

After each chosen card, the response to `BotDraftDraftPick` presents the next pack using the same `Payload` fields:

```log
<== BotDraftDraftPick(00000000-0000-4000-8000-000000000006)
{"CurrentModule":"BotDraft","Payload":"{\"Result\":\"Success\",\"EventName\":\"QuickDraft_MSH_20260702\",\"DraftStatus\":\"PickNext\",\"PackNumber\":0,\"PickNumber\":1,\"NumCardsToPick\":1,\"DraftPack\":[\"104933\",\"105063\",\"104949\",\"105020\",\"105036\",\"105003\",\"104989\",\"104948\",\"104971\",\"105134\",\"105086\",\"105007\",\"105166\"],\"PackStyles\":[],\"PickedCards\":[\"105097\"],\"PickedStyles\":[]}"}
```

## Chosen-card event

The chosen card is sent as a `BotDraftDraftPick` request. `PickInfo.CardIds` contains the selected `grpId`; Quick Draft picks one card, so use the first element.

```log
[UnityCrossThreadLogger]==> BotDraftDraftPick {"id":"00000000-0000-4000-8000-000000000006","request":"{\"EventName\":\"QuickDraft_MSH_20260702\",\"PickInfo\":{\"EventName\":\"QuickDraft_MSH_20260702\",\"CardIds\":[\"105097\"],\"PackNumber\":0,\"PickNumber\":0}}"}
```

The response `Payload.PickedCards` is a cumulative pool snapshot and can be used as a cross-check, but the request is the direct chosen-card event.

## Completion signal

The current log includes an explicit completion payload immediately after the final pick:

```log
<== BotDraftDraftPick(00000000-0000-4000-8000-000000000047)
{"CurrentModule":"DeckSelect","Payload":"{\"Result\":\"Success\",\"EventName\":\"QuickDraft_MSH_20260702\",\"DraftStatus\":\"Completed\",\"PackNumber\":2,\"PickNumber\":13,\"NumCardsToPick\":1,\"DraftPack\":[],\"PackStyles\":[],\"PickedCards\":[\"105030\",\"105097\",\"104989\",\"105134\",\"105003\",\"105054\",\"105037\",\"105070\",\"105054\",\"105117\",\"105014\",\"105084\",\"105034\",\"104997\",\"105037\",\"105006\",\"104996\",\"105047\",\"105032\",\"105003\",\"105017\",\"105049\",\"105003\",\"104983\",\"105005\",\"105013\",\"104998\",\"105033\",\"105000\",\"105031\",\"104995\",\"104986\",\"105004\",\"105164\",\"105005\",\"104995\",\"104911\",\"105117\",\"105053\",\"105182\",\"104989\",\"105002\"],\"PickedStyles\":[]}"}
Wotc.Mtga.Events.LimitedPlayerEvent:CompleteDraft()
```

The parser should auto-trigger the deck builder when `Payload.DraftStatus == "Completed"`. Fallback inference remains safe if Arena drops the explicit status: `PackNumber == 2`, `PickNumber == 13`, `DraftPack == []`, and 42 `PickedCards`.

## Human drafts

Premier, Traditional and Pick-Two drafts share one protocol. Draft Omen reads three kinds of line once the format is known.

### Pack offered

A `[UnityCrossThreadLogger]Draft.Notify` line carries a JSON object with:

- `draftId`: the draft's identifier.
- `SelfPack` and `SelfPick`: the pack and logical pick, both 1-based on the wire. Draft Omen stores them 0-based, so `SelfPack: 1, SelfPick: 1` is pack 0, pick 0.
- `PackCards`: the offered card `grpId` values.

The notify has no event name, so the pack belongs to the last draft event the parser detected. Arena logs a notify once or twice, and a repeat is dropped. A notify whose coordinates fall outside the format's pack rules is ignored.

### Pick submitted

A `==> EventPlayerDraftMakePick` request carries a string-encoded JSON `request` with `DraftId`, `Pack`, `Pick` and `GrpIds`. `Pack` and `Pick` are 1-based like the notify. `GrpIds` holds one or more selected cards: one in Premier and Traditional, two in Pick-Two. A pick whose card count does not match the format is still recorded as logged, and the parser writes a warning.

The response arrives as a `<== EventPlayerDraftMakePick(<id>)` marker followed by a JSON body on the next line. `IsPickSuccessful: false` drops the pick. `IsPickingCompleted: true` completes the draft.

When Arena fails a pick it can answer with the table state instead. That body is a JSON object with `TableInfo`, which holds `SelfPack`, `PickedCards` and `Players`, plus `PickInfo`, which holds `PackCards`, `SelfPack`, `SelfPick`, `NumCardsToPick` and `TimeoutSec`, and `PackInfo`. `PickedCards` does not include the failed pick. The client then sends a request for the same pack and pick again, and that request can name a different card. The parser treats this body like `IsPickSuccessful: false`: it drops the pending pick, and the resubmitted request records the pick. Any other JSON object after a pick response marker is ignored and never stops the parse.

A request with no logged response is still recorded when the next pack or completion arrives. A repeated request or response counts once.

### Draft completed

The draft completes once, from the first of:

- a pick response with `IsPickingCompleted: true`;
- a `DraftCompleteDraft` request whose `EventName` matches the draft and whose `IsBotDraft` is `false`;
- the `DraftCompleteDraft` response, whose `CardPool` lists the full pool Arena holds.

The completed pool holds the recorded picks. A missing pick stays a gap. When Arena's `CardPool` holds every recorded card plus the missing ones, Draft Omen saves that full pool instead, so a draft with gaps still keeps all 42 cards. A `CardPool` that lacks a recorded card leaves the pool unchanged, and the parser writes a warning.

### Resumed draft

When Arena restarts during a human draft, the new log has a login line but no `EventJoin` and no table draft queue line. The only record of the draft is a course in the `Courses` list of the `<== EventGetCoursesV2(<id>)` response. The body is one JSON line, `{"Courses": [...]}`, and the list holds many courses.

The parser resumes a draft from a course with `CurrentModule` set to `PlayerDraft`. The course needs a non-empty `CourseId`, `InternalEventName` and `DraftId`, and a `CardPool` that is a list of positive integers. A missing `CardPool` counts as empty. The event name must belong to a human-draft format, so a Quick Draft course never resumes. `DraftId` equals the `draftId` of the `Draft.Notify` lines that follow.

The parser adopts the course only when no draft is active and exactly one course qualifies. Arena also logs this list during a draft, and those later snapshots change nothing. A list with two qualifying courses is ambiguous and is ignored.

In real logs the `CardPool` is empty even at pack 2 or 3. After the list, Arena sends the `EventPlayerDraftMakePick` request for the current pick before any `Draft.Notify`, and notifies follow for the next picks. The first pack after the restart therefore carries only the picks made since the restart, plus the `CardPool` cards when the list has any.

Pack offers and the completion of a resumed draft carry `resumed: true`. The pool of a resumed pack may lack picks made before the restart, so the pool store checks only that the saved pool contains the completion picks. It also adds the `CardPool` cards it never saw as picks to the saved pool. The `CardPool` in the `DraftCompleteDraft` response holds all 42 cards and gives the full pool.

### Known limits

- No real Traditional drafting log has been captured. Traditional support rests on the synthetic fixture and the protocol it shares with Premier.
- Arena sometimes skips a `Draft.Notify` or a pick request. A skipped notify means no recommendation for that pick. A skipped request leaves a gap until the `CardPool` fills the pool.
- Malformed or unknown human-draft lines are ignored rather than failing the live session.

## Private logs

Real `Player.log` and `UTC_Log` files are private development inputs. They hold account identifiers, screen names, session ids and inventory. Keep them under `.git/private/player-logs/`, which git never tracks. That folder groups logs by format, with a `manifest.txt` in each subfolder. Its `public-samples/` subfolder holds anonymised excerpts collected from third parties. They are private too.

Run `uv run python scripts/verify_real_logs.py` to summarise every private log. It prints the format, packs, logical picks, cards, gaps and completion of each file, without card IDs or UUIDs.

Human-draft tests use hand-written synthetic fixtures such as `tests/fixtures/premier-draft-complete.log`, `traditional-draft-complete.log` and `pick-two-draft-complete.log`. The pseudonymised Quick Draft fixture predates this rule and is the only fixture taken from a real log. Do not commit a real log, an excerpt from one, an anonymised sample, or output derived from them.

## Rotation behavior

On macOS, the current log is at `~/Library/Logs/Wizards Of The Coast/MTGA/Player.log`. Arena rotates it on restart by moving the previous session to `Player-prev.log` and starting a new `Player.log`. Live recovery should scan `Player-prev.log` before `Player.log` on startup when attempting to reconstruct an in-progress draft.

## Reference cross-check

Reference implementations confirm the current fields and show token drift:

- `bstaple1/MTGA_Draft_17Lands` uses older marker strings `BotDraft_DraftStatus` and `BotDraft_DraftPick` in `src/constants.py`.
- `mtgatool/mtgatool-desktop` parses current `BotDraftDraftStatus` payload fields including `EventName`, `DraftStatus`, `PackNumber`, `PickNumber`, `DraftPack`, and `PickedCards`.
- `manasight/manasight-parser` documents current Quick Draft flow as `BotDraftDraftStatus` for initial pack presentation and `BotDraftDraftPick` for pick requests plus following pack responses.

No reference code was copied.

## PRD open questions answered

1. Current Quick Draft tokens are `BotDraftDraftStatus` for initial status and `BotDraftDraftPick` for pick requests/responses. The older underscore forms are not present in this fixture.
2. Account identity is available from `authenticateResponse.clientId`; use `screenName` or the login display-name line only as display metadata. Cross-patch stability is not provable from one fixture, so the documented fallback is `screenName` plus a manual account override if the token changes.
3. Completion is explicit via `Payload.DraftStatus == "Completed"` in `CurrentModule == "DeckSelect"`; fallback is final pick plus empty `DraftPack` plus 42-card `PickedCards` pool.
