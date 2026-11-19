- **Claims.** A script that passes `--ttl`, or runs `claim renew` or
  `claim cleanup`, now exits 2: drop `--ttl` and `cleanup`, and re-run
  `claim acquire` where it renewed. Claims already on a board need no step: a
  claim 0.7.0 or earlier wrote still lapses at its own `expires_at`, and those
  versions read a new claim as held. Since a new claim never lapses, have each
  lead release its workers' claims before it reports (#795).
