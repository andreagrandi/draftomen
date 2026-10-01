# Draft Omen for Moxgate

A Chrome extension that reads your draft on moxgate.com and sends each pack and
your pool to Draft Omen, so Draft Omen can show pick recommendations.

The extension only sends data to `127.0.0.1` on your own computer. It does not
contact any other server and does not read pages outside `www.moxgate.com`.

## Load it in Chrome

1. Open `chrome://extensions`.
2. Turn on Developer mode.
3. Click Load unpacked and pick the `extensions/moxgate` folder.

## Start Draft Omen

```
draftomen-tui watch --source moxgate
```

Draft Omen needs a Scryfall default-cards bulk file at startup. It uses the
file from `--bulk-file`, or else the Mocked Draft download in the app data
directory. If neither exists, watch exits with an error and does not start the
receiver. The receiver listens on port 47326. The extension has this port fixed,
so `--moxgate-port` only works if you also edit `RECEIVER_URL` in
`service_worker.js` and `popup.js`.

## Popup

Click the extension icon to see:

- "Draft Omen is running" when the receiver answers, or "Draft Omen is not
  running" with the start command when it does not answer within 2 seconds.
- The last snapshot. "Accepted" shows the pick number and the time. "Rejected"
  shows the error Draft Omen returned. "Not answered" means the post found no
  receiver.

The last result lives in session storage and is cleared when Chrome closes.

## Tests

```
npm ci --prefix extensions
node --test "extensions/tests/*.test.js"
```

Or run `uv run nox -s extension`. The tests load synthetic HTML fixtures from
`extensions/tests/fixtures` with jsdom.
