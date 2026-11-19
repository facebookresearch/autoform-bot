- **`autoform claim renew`, `--ttl` and `claim cleanup`**, deprecated in 0.7.0,
  are gone, and each is now an argparse error (exit 2). A new claim is held
  until it is released: its lease records an `expires_at` of
  3000-01-01T00:00:00Z. Re-run `claim acquire` on the same set to confirm it,
  and free a stopped owner's claim with `claim release --worker-id OWNER
  TARGET`. `project worktrees` gives the time each claim was acquired instead
  of the time it expires (#795).
