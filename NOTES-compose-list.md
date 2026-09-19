# Android fixture: a Compose list beside the RecyclerView one

Parked branch. Nothing implemented yet.

## Why

`u2_client.scroll_into_view` says it avoids `UiScrollable.scrollIntoView`
because that "no-ops on Compose `LazyColumn`" (#50), and names LazyColumn among
the containers it handles. The fixture has no Compose in it at all -- its
dependencies are appcompat, material, viewpager2 and recyclerview -- so the
main stated reason for the Android sweep's design has never had a fixture
behind it. Hybrid apps are the norm, and this is the half we cannot see.

## Shape agreed

A switch *inside* the Scroll tab, not a sixth tab:

- keeps the tab bar symmetric with iOS, where the fifth tab already spills
  into "More";
- makes it the same list in two implementations, so the scroll tests
  parametrise over the mode and a difference is attributable to it;
- the Compose rows can carry real per-row ids via `Modifier.testTag` with
  `testTagsAsResourceId`, so one platform covers both lookup styles: the
  RecyclerView keeps the realistic shared-id shape, and the Compose list
  exposes `row_41` the way iOS does.

The risk is a mode toggle that silently tests the wrong implementation, so the
readout names the active one (`recycler` / `compose`) and the tests assert it
before scrolling rather than trusting the tap.

## Cost

Gradle: compose BOM, ui, material3, activity-compose, `buildFeatures.compose`.
Then a `ComposeView` in the scroll fragment and the toggle. Most of the work is
Gradle, and it slows the fixture build.

## Do this after #232

The Android scroll tests are red on #232 (the sweep cannot reach past ~110 rows
and never turns around at the end of a list). Adding a mode into already-red
tests makes both harder to read.

## Also worth doing, for symmetry

iOS has the same split -- `UITableView` versus SwiftUI `List` -- and the
fixture only has the former.
