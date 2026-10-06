# Archive skill source provenance

## Artifact

- Filename: `math_lean_skills_agent_config_2026-08-31.zip`
- Source: archive supplied through an authenticated object store during the
  original review session
- Attested SHA-256:
  `9d38fe39237afdf673073fd6ebeb15f01514f033689edd56ba3b3251d611d7d3`
- Externally attested inventory: 335 entries, including 51 `SKILL.md` files

The archive itself is not checked into AutoformBot.

## Evidence limitation

This repository contains neither a durable object-store locator/version nor a
generated ZIP-member inventory. The aggregate counts, SHA comparison, safe-path
scan, and license-filename scan are therefore an **external attestation from the
original authenticated review**, not independently reproducible repository
evidence. Repository tests verify only that the 51 declared policy records are
unique and internally consistent with the human plan.

Do not describe this as a verified 51/51 source inventory. A future review may
upgrade the evidence only by recording a durable immutable object identifier
and committing a deterministic member inventory with normalized path, member
type, size, CRC or content hash, `SKILL.md` membership, and license-filename
candidates. The archive bytes still must not be redistributed here.

## Reuse status

The external scan reported no filename candidate named `LICENSE`, `LICENCE`,
`COPYING`, `NOTICE`, or `README` under its documented case-insensitive matching
rule. That is not proof that the material has no license or that reuse is
authorized. The request authorized analysis and planning, not verbatim copying
into this MIT-licensed repository.

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

## Attested review method

The original reviewer reported reading the archive through an authenticated
object-store client, matching the supplied SHA-256, and checking ZIP member
names for absolute paths, parent traversal, and symbolic links before
extraction. Those claims cannot be regenerated from this repository today.

The transport manifest records 51 declared skill decisions exactly once. Tests
compare those decisions with the numbered review table, verify their declared
totals, reject duplicate JSON keys, and enforce the no-verbatim-copy policy.
