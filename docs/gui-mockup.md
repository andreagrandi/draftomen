# Desktop GUI

The PySide6/QML desktop application is Draft Omen's default interface. It
uses the same immutable session state and explicit commands as the terminal
frontend. A Qt adapter schedules live session work outside the GUI thread and
publishes plain Qt values and item models to presentation-only QML.

## Launch

Install Draft Omen and launch the live provider:

```bash
uv run draftomen
```

The live provider follows Arena's standard `Player.log` location and uses the
shared set-profile lifecycle as its ratings authority. It starts with the
production hosted manifest
`https://www.draftomen.com/profiles/manifest.json` unless an explicit
`--profile-manifest-url URL` override is supplied. Use `--log-path` to override
the platform-default log location.

The native profile flags are:

```bash
uv run draftomen
uv run draftomen --profile-manifest-url "$PROFILE_MANIFEST_URL"
uv run draftomen --offline-profiles
```

`--offline-profiles` selects `ProfileNetworkPolicy.OFFLINE` for profile
networking and takes precedence over the hosted profile configuration. It is
profile-only offline mode, not full application offline mode: Scryfall card
metadata, card images, and static card-data networking keep their own
cache/network policies. A valid local profile is used immediately; if hosted
data is unavailable or invalid, the last usable cache is retained, with
deterministic fallback scoring when no usable empirical profile exists. Native
live sessions never load ratings directly from 17Lands.

The existing ratings refresh control in Settings requests the same shared
hosted-profile refresh as the terminal `d` action. The native view presents the
shared session outcome: `updated` adopts a newer validated profile and updates
recommendations in place, while `unchanged` leaves current ratings active.
Failed, offline, or missing refreshes retain the last usable cache when ratings
exist; with no usable empirical profile, deterministic fallback scoring remains
active. 17Lands remains visible as ratings attribution; it is not a direct
runtime ratings source.

Select the deterministic provider for an automated smoke check without
filesystem or network dependencies:

```bash
uv run draftomen --provider mock --smoke-test
```

For visual development, use the explicit forced-mock entry point:

```bash
uv run draftomen-gui-mockup
```

Use the selectors to open a specific surface, representative state, or
responsive target:

```bash
uv run draftomen-gui-mockup --surface build --width 1440 --height 900
uv run draftomen-gui-mockup --surface live --width 760 --height 900
uv run draftomen-gui-mockup --surface backtest --scenario error
```

The mock-only top-bar selector switches between `loading`, `ready`, `empty`,
`progress`, `warning`, and `error`. Primary navigation opens Live Draft, Deck
Build, and Backtest. Settings remains available from the top bar in both
provider modes.

To capture a screenshot during the smoke check, add
`--screenshot /tmp/draftomen-smoke.png` before the process exits.

## Responsive behavior

- **Wide, 1440 × 900:** persistent navigation, ranked recommendation workspace, selected-card preview, and pool summary remain side by side. Deck Build keeps the focused-card panel beside the build sections.
- **Narrow, 760 × 900:** persistent desktop navigation becomes compact, recommendation rows stack their secondary facts, and Card details and Pool use an explicit segmented view. Deck Build removes the permanent preview column while retaining summary and rebuild controls.

Resize the running window across the breakpoint to review both arrangements; no restart is required.

## Recorded visual direction

The implementation follows the checked-in **Draft Omen** design system and the selected Stitch references recorded in `gui-design-plan.md`:

- deep midnight-navy tonal surfaces;
- luminous periwinkle reserved for recommendations, active navigation, and successful status;
- champagne gold for warnings and accents, with warm ivory primary text;
- compact, desktop-native information density with crisp outlines and restrained radii;
- visibly distinct recommended, selected, and keyboard-focus treatments;
- persistent read-only status and 17Lands attribution;
- neutral labelled card-image placeholders rather than generated Magic artwork.

The generated HTML under `ui_mockups/` remains reference material only. QML
renders published `set_profile` and ratings state and emits explicit user
intents. It does not perform networking, scoring, caching, or lifecycle
decisions. The Qt adapter owns the shared session boundary, including one
`ProfileClient` shared by the live session and its background refresh worker;
parsing, persistence, scoring, builds, backtests, and recovery remain in
shared Python.

