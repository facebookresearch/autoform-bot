# Read-back faithfulness rubric

Judge whether an independent read-back asserts the same mathematics as the
source passage. The judge sees English evidence, not Lean. This is the blind
half of [faithfulness](faithfulness.md): that rubric compares Lean with the
source; this one compares testimony about Lean with the source.

## Roles and artifacts

Keep the three roles separate.

1. A **trusted coordinator** builds the exact candidate and runs skeleton
   extraction once with its report, packets, and passages. Extraction produces
   Lean packets, source passages, and their manifests. It does not produce a
   read-back or a verdict.
2. A **blind auditor** receives one declaration packet and no article, source,
   locator, or statement of intent. The auditor returns a nonempty UTF-8
   Markdown account of what that packet literally asserts. This returned text
   is the read-back artifact.
3. A **faithfulness judge** receives the source passage, the raw read-back text
   for every declaration in the article, and the opaque provenance projection
   below. The judge must not receive a packet, Lean name, article path, or
   candidate checkout.

The coordinator, not either reviewing agent, supplies the evidence, taken from
the verified review bundle and its read-back cards. The judge only returns its
JSON and never writes into the reviewed repository. The coordinator maps the
packet manifest's `node_id` to that blueprint node's `article_id`; an absent or
duplicate identity stops dispatch. Hash the read-back bytes exactly as the
auditor returned them; do not render or normalize them before hashing.

Before dispatching the judge, build one opaque item:

<!-- readback-faithfulness-item-template -->
```json
{
  "schema": "autoform-readback-faithfulness-item/v1",
  "item": "af_<24 lowercase hex digits>",
  "declarations": [
    {
      "id": "d1",
      "skeleton_hash": "sha256:<64 lowercase hex digits>",
      "packet_hash": "sha256:<64 lowercase hex digits>",
      "read_back_hash": "sha256:<64 lowercase hex digits>"
    }
  ],
  "article_skeleton_hash": "sha256:<64 lowercase hex digits>",
  "article_packet_hash": "sha256:<64 lowercase hex digits>",
  "article_review_hash": "sha256:<64 lowercase hex digits>",
  "passage_hash": "sha256:<64 lowercase hex digits>"
}
```

`d1`, `d2`, and so on follow the declaration order in the packet manifest. The
coordinator supplies read-backs in that same order. It verifies that the report
and all packet entries agree on the node, declaration list, article skeleton,
article packet, and review hashes, that the passage manifest has the same node
and review hash, and that every packet, passage, and read-back file matches its
recorded hash. A missing source passage, duplicate or unordered declaration, or
disagreeing record makes the item unavailable; do not ask the judge to repair
it.

The declaration and article skeleton hashes bind elaborated meaning and trust
context. The packet hash binds the exact declaration packet seen by its auditor.
The article packet hash binds the joint packet. The article review hash also
binds that joint packet to the cited passage and locator. None of those hashes
binds the read-back, so the read-back hash is required. These are drift
checksums, not reviewer authentication or an approval key.

## Evidence boundary

The judge receives exactly three kinds of input:

1. the opaque item JSON;
2. the cited source passage as raw text;
3. each declaration's raw read-back text, labelled only `d1`, `d2`, and so on.

Do not open other files, resolve a declaration name, or search the repository.
Judge all read-backs for one item together: one source theorem may be split
into existence and uniqueness declarations that are incomplete in isolation.

Read both mathematical texts as raw text, not rendered output. A zero-width or
bidirectional character, control code, or TeX construct such as `\phantom` can
make rendered content differ from the bytes under review. Record `unreadable`
rather than carding around hidden content.

## Procedure

1. Copy the item's provenance fields into the verdict without changing them.
2. Card the passage: objects and kinds, numbered hypotheses, conclusion, and
   quantifier order. Mark a hypothesis implicit only when the passage actually
   relies on it.
3. Card the combined read-backs in the same form.
4. List every discrepancy with exactly one category from the table. The worst
   category determines the decision; implication in one direction is not
   equivalence.

A read-back is expected to spell out binders, conventions, and degenerate
behavior already determined by the same claim. That is `elaboration`. Extending
the domain, removing a hypothesis, strengthening a conclusion, or adding a
nonredundant assumption changes the claim and is not elaboration.

## Categories and decisions

| Category | Decision | Meaning |
|---|---|---|
| `none` | `agrees` | The cards state the same claim. |
| `elaboration` | `agrees` | Extra wording makes an implicit binder, convention, or logically redundant implementation detail explicit without changing the mathematical instances or conclusion. |
| `equivalent-reformulation` | `review` | The forms appear mathematically equivalent, but the equivalence needs independent confirmation. State it in words. |
| `hypothesis-missing` | `disagrees` | A source hypothesis is absent from the read-back. This makes the read-back stronger unless another mismatch intervenes; a stronger theorem is still not the same statement. |
| `hypothesis-added` | `disagrees` | The read-back has an additional nonredundant assumption that the passage neither states nor implicitly requires. This makes the read-back weaker. |
| `conclusion-weaker` | `disagrees` | The read-back conclusion says less, such as existence instead of unique existence. |
| `conclusion-stronger` | `disagrees` | The read-back conclusion says more. Proving more does not make the formalized statement identical to the cited one. |
| `conclusion-different` | `disagrees` | Neither conclusion entails the other as stated. |
| `quantifier` | `disagrees` | Quantifier order, strength, or dependence changed. |
| `strictness` | `disagrees` | Strict became non-strict, open became closed, or positive became nonnegative, or conversely. |
| `domain` | `disagrees` | Type, domain, finiteness, locality, or covered edge cases changed. |
| `object-substituted` | `disagrees` | A passage object became a proxy that the read-back does not connect to it. |
| `scope` | `disagrees` | The read-backs cover only part of what the passage claims. Name the omitted part. |
| `vacuous` | `disagrees` | The read-back says the hypotheses cannot hold or the claim is trivial for an unintended reason. |
| `unreadable` | `disagrees` | The text cannot be carded as supplied, including content hidden from rendered output. |
| `evidence-missing` | `unknown` | The passage, a read-back, or required provenance is absent or malformed. |

Use `elaboration` for an added assumption only when it is plainly redundant or
an implementation-level restatement and the detail line says why it changes no
mathematical instance. Otherwise use `hypothesis-added`. Use
`equivalent-reformulation`, not `elaboration`, when equivalence itself needs an
argument. There is no special pass for a missing hypothesis that generalizes
the source: substantive strengthening is `hypothesis-missing` and disagrees.

An item takes the worst decision among its discrepancies:
`disagrees` outranks `unknown`, which outranks `review`, which outranks `agrees`.
Use `unknown` only when evidence is unavailable. A present but unrelated claim
is `conclusion-different`, not unknown.

## Exact output

Return one JSON object per item and nothing else. Copy all provenance values and
the declaration order verbatim from the input item. Use `null` for
`equivalence_to_settle` unless the decision is `review`.

<!-- readback-faithfulness-verdict-template -->
```json
{
  "schema": "autoform-readback-faithfulness-verdict/v1",
  "item": "af_<24 lowercase hex digits>",
  "declarations": [
    {
      "id": "d1",
      "skeleton_hash": "sha256:<64 lowercase hex digits>",
      "packet_hash": "sha256:<64 lowercase hex digits>",
      "read_back_hash": "sha256:<64 lowercase hex digits>"
    }
  ],
  "article_skeleton_hash": "sha256:<64 lowercase hex digits>",
  "article_packet_hash": "sha256:<64 lowercase hex digits>",
  "article_review_hash": "sha256:<64 lowercase hex digits>",
  "passage_hash": "sha256:<64 lowercase hex digits>",
  "passage_card": {
    "objects": ["<object and kind>"],
    "hypotheses": ["1. <hypothesis>"],
    "conclusion": "<conclusion>",
    "quantifiers": "<order and dependence>"
  },
  "read_back_card": {
    "objects": ["<object and kind>"],
    "hypotheses": ["1. <hypothesis>"],
    "conclusion": "<conclusion>",
    "quantifiers": "<order and dependence>"
  },
  "discrepancies": [
    {"category": "none", "detail": "cards agree"}
  ],
  "equivalence_to_settle": null,
  "decision": "agrees",
  "verdict": "cards agree"
}
```

## Review escalation

For `equivalent-reformulation`, state the proposed equivalence in words and
hand it to a reviewer allowed to inspect Lean. That reviewer may prove the
equivalence or reject it. Do not use `review` for a partial result, a stronger
result, or a claim described only as "roughly" or "essentially" the same.

A wide source locator is an article defect, not an auditor defect. If the
passage contains several claims and the read-backs cover only one, record
`scope`; the remedy is to narrow the citation or formalize the rest.
