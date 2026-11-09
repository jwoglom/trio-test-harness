
## The setup did not finish

The app was built, installed and launched, but a step of the setup after that failed, so the app may not be in the
state described above:
{problems}
That failure is the harness's, not the PR's. Look at the screen first, finish what is missing (for example
`./trio-test-harness --target sim launch` to restart the app, `./trio-test-harness --target sim flow back-home`, or
`./trio-test-harness --target sim flow add-cgm-sim`), then test as usual. Say in your report what you had to finish,
and record a harness problem you could not get past with `./trio-test-harness run note`.
