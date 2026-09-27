# Microsoft Store submission

This page holds everything the first Partner Center submission needs, and the
record of what was submitted. Later releases go through the automated update
flow in #329, which only works once this first release is live.

## Package to submit

Submit the `.msixupload` built by the release workflow for the tag, not a
development build. The release run calls `native-bundles.yml`, whose Windows
job builds the package, installs a test-signed copy, runs the smoke test from
the installed app and removes it again. The job then uploads the
`draftomen-windows-msixupload` artifact. See
[MSIX build in GitHub Actions](desktop-bundles.md#msix-build-in-github-actions).

1. Open the release run for the tag in the **Actions** tab and download the
   `draftomen-windows-msixupload` artifact.
2. Extract it. It holds `DraftOmen_X.Y.Z.0_x64.msixupload` and
   `DraftOmen_X.Y.Z.0_x64.msix.sha256`.
3. Check that the `.msix` inside the upload matches the recorded checksum:

   ```bash
   unzip DraftOmen_X.Y.Z.0_x64.msixupload
   shasum -a 256 -c DraftOmen_X.Y.Z.0_x64.msix.sha256
   ```

4. Upload the `.msixupload` file itself on the **Packages** page. Partner
   Center reads the identity, version and architecture from it. They must be
   `27809AndreaGrandi.DraftOmen`, `X.Y.Z.0` and `x64`.

Actions artifacts expire, so download the artifact soon after the release run.

## Pricing and availability

- Pricing: free. The automated update flow in #329 supports free products only.
- Markets: every market Partner Center offers by default.
- Visibility: public and discoverable in the Store.
- Release: publish as soon as certification passes.

## Properties

- Category: Utilities + tools.
- Privacy policy URL: <https://www.draftomen.com/privacy/>. The app downloads
  card data, so the Store requires one.
- Website: <https://www.draftomen.com/>.
- Support contact: <https://github.com/andreagrandi/draftomen/issues>.
- Product declarations: the app does not collect personal data for the
  publisher and has no in-app purchases or ads.
- System requirements: keyboard and mouse. The package targets x64 Windows 10
  1809 or later.

## Age ratings

Answer the IARC questionnaire as an app, not a game. Draft Omen has no chat,
no user-generated content, no purchases and no location sharing. It shows
Magic: The Gathering card images from Scryfall, and card art can show fantasy
combat. Answer the violence questions for that artwork honestly rather than
aiming for a particular rating.

## Store listing

Language: English (United States).

### Product name

Draft Omen

### Description

```text
Draft Omen is an unofficial draft assistant for MTG Arena Quick Drafts. It reads Arena's local log while you draft, recognizes each pack and pick, and ranks the cards on offer. When the draft ends, it suggests a 40-card deck from your pool.

Every recommendation shows the data behind it: the DO Score, the 17Lands win rate and grade, how the card fits your colors, and a summary of your pool so far. You can sort by other measures to compare.

Draft Omen is read-only. It never writes to, injects into or automates MTG Arena. It needs Detailed Logs (Plugin Support) turned on in Arena's account settings.

Draft Omen is unofficial Fan Content permitted under the Fan Content Policy. Not approved/endorsed by Wizards. Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC. Card data from Scryfall and 17Lands; neither service endorses this tool.
```

### Product features

- Ranks every card in the current pack while you draft.
- Shows the DO Score, 17Lands win rate and grade for each card.
- Tracks your pool's colors, mana curve and recent picks.
- Suggests a 40-card deck with a mana base when the draft ends.
- Reads Arena's log only and never changes the game.

### Search terms

MTG Arena, Quick Draft, draft assistant, 17Lands, deck builder

### Copyright and trademark info

```text
Draft Omen is not affiliated with, sponsored by, approved by, or endorsed by Wizards of the Coast, Scryfall, or 17Lands. Magic: The Gathering and MTG Arena are trademarks of Wizards of the Coast LLC.
```

### Screenshots

The screenshots in `docs/assets/microsoft-store/` come from the real app
rendering a recorded MSH Quick Draft at 1920 by 1080. The Store requires at
least 1366 by 768.

| File | Shows | Card art |
|---|---|---|
| `01-live-draft.png` | Pack 1, pick 3 with ranked cards and pool summary | Yes |
| `02-suggested-deck.png` | The suggested 40-card deck | Yes |
| `03-suggested-deck-no-card-art.png` | The same deck with **Card image preview** off | No |
| `04-settings.png` | Draft guidance and display settings | No |

MSH card art belongs to Wizards of the Coast and Marvel. If certification
objects to third-party artwork, replace the listing screenshots with `03` and
`04`, which show no card art.

### Logos

The Store takes the tile logos from the package. No separate listing logo is
needed for the first submission.

## Submission options

### Restricted capability justification

The package declares `runFullTrust`. Paste this into the justification field:

```text
Draft Omen is a desktop app built with Qt and packaged as a full-trust Win32 application. It needs runFullTrust to run as a classic desktop process and to read MTG Arena's Player.log in the user's AppData\LocalLow folder while the user drafts. It only reads that file. It does not write to other applications, install services or drivers, or change system settings.
```

### Notes for certification

```text
Draft Omen is a companion app for MTG Arena Quick Drafts. It reads Arena's local Player.log and shows pick recommendations. It never writes to Arena.

Testing without MTG Arena: launch the app. It opens on the Live Draft view with an "Arena setup needed" message that explains how to turn on Arena logs, because no Arena log exists. A "Ratings unavailable" notice is expected too, because ratings load only after the app detects a draft's card set. The About and Privacy buttons in the left rail work without Arena.

Testing with MTG Arena: in Arena, open Settings > Account, turn on Detailed Logs (Plugin Support) and restart Arena. Start a Quick Draft. Draft Omen follows each pick, ranks the cards in the pack, and suggests a deck on the Deck Build view when the draft ends.

Network use: the app downloads set rating profiles from www.draftomen.com and card data and images from Scryfall. It has no account system and sends no personal data. The privacy policy is at https://www.draftomen.com/privacy/.
```

## Publication record

Fill this in as the submission moves through Partner Center.

| Field | Value |
|---|---|
| Release tag | |
| GitHub Actions run | |
| Package version | |
| `.msix` SHA-256 | |
| Submitted on | |
| Certification result | |
| Reached **In the Store** on | |
| Signed by Microsoft | |
| Public Store URL | `https://apps.microsoft.com/detail/9NPCD3VLZQMX` |

## Store acquisition check

GitHub-hosted runners cannot install apps from the Microsoft Store client, so
nothing in CI proves that the live listing installs and launches. That check
runs by hand on an external Windows 11 device before the Windows release
channel moves to the Store in #330. It is not part of this first submission.
