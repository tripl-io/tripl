# Reproduce a missing purchase

From this directory, using a development install of the Tripl CLI and an empty output directory:

```sh
captures=$(mktemp -d)
for build in healthy suspect fixed; do
  tripl journey-capture --journey checkout --fixture synthetic-order-v1 \
    --build "$build" --output "$captures/$build.json" --allow-event purchase \
    -- python3 checkout.py "$build" || exit 1
done

tripl reproduce --event purchase --healthy "$captures/healthy.json" \
  --suspect "$captures/suspect.json"

tripl reproduce --event purchase --healthy "$captures/healthy.json" \
  --suspect "$captures/suspect.json" --candidate "$captures/fixed.json"
```

The first comparison exits 3 when it reproduces the regression. The second exits 0 when the candidate restores the expected count.

For an application pilot, run the capture step inside each build's CI job with its immutable build identifier. Substitute the team's existing deterministic checkout test for the example. Have the test assert successful checkout and write a JSON array of observed event names to the file named by `TRIPL_JOURNEY_EVENTS`. Only explicitly allowlisted event names enter the capture. The adapter runs the command twice with separate output files and a five-minute timeout per attempt. Use a synthetic account and reset the fixture between attempts. Upload the resulting JSON as a CI artifact for the incident comparison.
