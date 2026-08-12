# GlobalProtect VPN Connect — local fork

**Temporary local copy** of [`atlanhq/github-actions/globalprotect-connect-action`](https://github.com/atlanhq/github-actions/tree/main/globalprotect-connect-action),
with one addition: a `computer-name` input that is forwarded to
`openconnect --local-hostname=<name>` (the GP backend advertises that
value as the `computer` field during auth).

## Why this exists

Without `--local-hostname`, openconnect sends the runner's default
hostname (`uname -n`) as the GlobalProtect client identity. Two logins
with the same `(user, client-identity)` collide — GP force-logs-out the
older session. That's what tore down the tunnel mid-poll in run
[25492349986](https://github.com/atlanhq/native-migration-app/actions/runs/25492349986)
(VPN snapshot went `tun_iface=up` → `tun_iface=absent` ~90s into polling,
followed by `403 IP address not allowed`).

A unique `computer-name` per job — e.g. `gh-<repo_id>-<run_id>-<run_attempt>-<job_index>-<tenant>` —
gives each runner its own GP session that nothing else can invalidate.

## Plan

1. ✅ Verified in run [25495226010](https://github.com/atlanhq/native-migration-app/actions/runs/25495226010):
   tunnel held `tun_iface=up` across the full poll cycle, workflow reached
   `COMPLETED`, report parsed cleanly.
2. PR the same change upstream to `atlanhq/github-actions` and switch
   this workflow back to
   `atlanhq/github-actions/globalprotect-connect-action@<tag>`.
3. Delete this directory.

## Inputs

| Input           | Description                                                                                        | Required |
|-----------------|----------------------------------------------------------------------------------------------------|----------|
| `portal-url`    | GlobalProtect portal URL                                                                           | Yes      |
| `username`      | GlobalProtect username                                                                             | Yes      |
| `password`      | GlobalProtect password                                                                             | Yes      |
| `max-attempts`  | Connection attempts before failing (default `3`)                                                   | No       |
| `computer-name` | Unique GP client identity forwarded to `openconnect --local-hostname`. Required for matrix/parallel use. | No       |

## Outputs

| Output          | Description                                       |
|-----------------|---------------------------------------------------|
| `vpn-connected` | Whether the VPN connection was established (`true`) |
