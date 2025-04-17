# Problems for Customer Support in Current Setup

## Long Running Workflows

Long running workflows are a problem, no customer wants to wait more than 24 hours for Atlan to sync with their data estate.

If a workflow is running for longer, we don't know until we go and inspect the workflow if it's the publish step that is taking the most time or the extract. So all "workflow taking longer" or "workflow failed" tickets have this split responsibility -- and it takes extra steps to figure out if it's to be redirected to the engineering team or the customer success team.

## Current State Sync Issues

- The way the diff implementation works is that it takes uses a dump of all the assets transformed in the previous workflow run and then performs the diff.
- The dump is exported when the publish step completes successfully (and publish steps are usually long running and run into errors).
- This leads to a difference in the state on Atlas and the state the workflows use to figure out the diff.

## Edge Cases in Diff and Publish

Diff and publish also have plenty of edge cases based on the current use cases we have.