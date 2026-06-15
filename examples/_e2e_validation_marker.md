# e2e validation marker (throwaway)

This file exists only so the validation PR touches a `code`-filter path,
satisfying the `build-sdk-base-image` job gate (`changes.outputs.code == 'true'`)
while the real change under test is the Dockerfile `APP_RUNTIME_BASE_E2E_MARKER`.

Delete with the branch once the image-level e2e run is verified.
