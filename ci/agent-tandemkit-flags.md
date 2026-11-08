
## TandemKit's feature flags

TandemKit puts features that are still being validated behind feature flags, saved with the pump's state. A flag
that is off hides its feature entirely, so a PR that adds or changes something behind a flag can only be tested with
that flag on; test its default too when the PR changes what users see with the flag off. The flags are on TandemKit's
settings screen ▸ View Debug Options ▸ Feature Flags, and that screen hides the list behind the pump image while every
flag is at its default (a triple tap on the image shows it). These commands do all of that:
- `./trio-test-harness --target sim flow tandem-feature-flags`: opens the Feature Flags screen from anywhere in the
  app and shows the list.
- `./trio-test-harness --target sim flow tandem-feature-flag <rawValue or title> [--off]`: turns one flag on (or off)
  and records the screen. Add `--from-pump-settings` when TandemKit's settings screen is already showing, or `--here`
  when the Feature Flags screen is.
A flag stays as you set it until you set it back. Say in your report which flags you changed.

The flags in this build (rawValue, title, default):
{flags}
