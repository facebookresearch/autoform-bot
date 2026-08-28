# Synchronize an Autoform project with Zulip

Use this workflow only when the user opts into Zulip discovery or project
coordination. Treat reading and writing as separate permissions: research is
read-only, and drafting a post does not authorize sending or editing one.

## Discover community context

Look for `.zuliprc` in standard local locations without printing or copying its
credentials. If none is available, ask the user to provide its path or configure
one. Search only relevant topics and report overlapping work, active
contributors, design rationale, and useful references.

Before tagging contributors, resolve their exact active Zulip identities; do not
guess from GitHub names or handles. Confirm the intended stream and whether the
request is for a new topic or an existing discussion.

## Draft a coordination message

Ground the wording in the actual project state. Present ongoing work as
underway, and invite people who have already thought about the design to sync so
the project does not duplicate or scoop existing efforts or step on
contributors' toes. Avoid language that accidentally claims an area or suggests
that established work has not begun.

Include only what helps coordination:

- the purpose and current status;
- the repository and overlapping work, with exact contributor mentions;
- the proposed scope, architecture, and compatibility goals;
- genuine open design, placement, or contribution questions;
- the principal sources; and
- any authorship or AI-polishing disclosure the user requests.

## Send and verify

Send, edit, or otherwise mutate Zulip only after explicit user approval. Use the
authenticated API or CLI without exposing credentials. After sending, fetch the
message and verify the stream, topic, links, and rendered mentions; a successful
API response does not prove that names resolved or that the social framing is
correct. Edit misleading wording when authorized, and report the final topic or
message identifier.
