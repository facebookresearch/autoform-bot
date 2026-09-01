# Archive skill source provenance

## Artifact

- Filename: `math_lean_skills_agent_config_2026-08-31.zip`
- Source: archive supplied by the user through an authenticated object store
- User-supplied SHA-256:
  `9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3`
- Downloaded SHA-256: identical to the user-supplied value
- Archive inventory: 335 entries, including 51 `SKILL.md` files

The archive itself is not checked into AutoformBot.

## Reuse status

The archive contained no file named `LICENSE`, `LICENCE`, `COPYING`, `NOTICE`,
or `README` establishing a reuse license. The user's request authorizes analysis
and planning, but it is not treated as a grant to copy the archive verbatim into
this MIT-licensed repository.

Therefore the default policy is independent reimplementation:

- archive text and scripts are design evidence, not source files;
- portable behavior is specified in Autoform's own words and architecture;
- no internal endpoint, credential, employee identifier, machine path, model
  entitlement, or private service procedure may enter the public runtime;
- a future verbatim transfer requires a separately recorded authorization and
  license-compatibility decision; and
- absence of a license file must never be interpreted as permission to copy.

The machine-readable policy is
[`skills/archive-transport-manifest.json`](skills/archive-transport-manifest.json).
The human review and planned PR boundaries are in
[`ARCHIVE_SKILL_TRANSPORT_PLAN.md`](ARCHIVE_SKILL_TRANSPORT_PLAN.md).

## Verification method

The artifact was first requested through its web explorer URL, which returned
an authentication HTML page rather than the ZIP. It was then read through the
authenticated object-store client, and the downloaded bytes were accepted only
after the SHA-256 matched exactly. The ZIP member names were checked for
absolute paths, parent traversal, and symbolic links before extraction; none
were found.

The transport manifest records all 51 skills exactly once. Tests compare the
manifest with the numbered review table, verify the declared disposition
totals, and enforce this no-verbatim-copy policy before later transport work.
