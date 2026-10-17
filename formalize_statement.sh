#!/usr/bin/env bash
# Run AutoformBot on a single mathematical statement instead of a whole paper.
#
#   ./formalize_statement.sh "6 is not a prime number"
#   ./formalize_statement.sh --roadmap-only "every finite group of prime order is cyclic"
#   ./formalize_statement.sh --project ~/my-lean-project "..."
#   ./formalize_statement.sh --propose --mission-title "Six is composite" "6 is not a prime number"
#   ./formalize_statement.sh --prove2me Positive_definite_matrix_integral_inequality
#
# Statements go into one shared Lean/Mathlib workspace (default:
# ~/autoform_statements, created on first use) so the Mathlib build cache is downloaded only once.
# Each statement becomes one roadmap article plus one Lean file, driven by a
# headless Claude Code session with this checkout loaded as the plugin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${AUTOFORM_STATEMENTS_DIR:-$HOME/autoform_statements}"
PACKAGE="Statements"
ROADMAP_ONLY=0
MODEL=""
PROVE2ME=""
PROPOSE=0
MISSION_TYPE=""
MISSION_TITLE=""
DRY_RUN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] "<statement>"
       $(basename "$0") --propose [--mission-title T] [--dry-run] [options] "<statement>"
       $(basename "$0") --prove2me MISSION [--dry-run] ["<hint>"]

Formalize and prove a single statement with AutoformBot, or contribute a
proof to a Prove2Me mission.

Options:
  --project DIR    Lean+Mathlib project to work in (default: $PROJECT).
                   Created with 'lake init' if it does not exist; an existing
                   project must already depend on Mathlib.
  --roadmap-only   Write the blueprint article only; do not touch Lean.
  --model MODEL    Claude model to use (default: Claude Code's default).

Proposing a Prove2Me mission from the statement:
  --propose        After formalizing, draft a Prove2Me mission proposal for
                   the statement in the platform's default Mathlib: blind
                   read-backs, a mission description, a local build, then
                   'autoform prove2me propose'. The proposal stays a private
                   draft until you confirm its items and click Submit
                   Proposal on prove2.me (homepage > My missions).
  --mission-type T OpenProblem, Textbook, or ResearchPaper (default: Claude
                   picks the one that fits the statement).
  --mission-title T  Mission name (default: Claude chooses one).

Prove2Me:
  --prove2me MISSION  Pick an open frontier theorem of this Prove2Me mission
                   (mission URL, URL slug, or name) and submit a proof or
                   proof sketch. An optional "<hint>" says which theorem or
                   approach to prefer. The first run clones the Prove2Me
                   workspace, asks for your API key, and fetches the
                   mission's Mathlib (one checkout per Mathlib version, under
                   ~/prove2me_workspace or \$PROVE2ME_WORKSPACE).
  --dry-run        With --prove2me or --propose: check and compile locally,
                   but do not submit or upload anything.
  -h, --help       Show this help.
EOF
}

STATEMENT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --roadmap-only) ROADMAP_ONLY=1; shift ;;
    --model) MODEL="$2"; shift 2 ;;
    --prove2me) PROVE2ME="$2"; shift 2 ;;
    --propose) PROPOSE=1; shift ;;
    --mission-type) MISSION_TYPE="$2"; shift 2 ;;
    --mission-title) MISSION_TITLE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; STATEMENT="$*"; break ;;
    -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) STATEMENT="${STATEMENT:+$STATEMENT }$1"; shift ;;
  esac
done

if [[ -z "$STATEMENT" && -z "$PROVE2ME" ]]; then
  usage >&2
  exit 2
fi

if [[ "$PROPOSE" -eq 1 ]]; then
  [[ -z "$PROVE2ME" ]] || { echo "error: --propose and --prove2me are separate modes" >&2; exit 2; }
  [[ "$ROADMAP_ONLY" -eq 0 ]] || { echo "error: --propose needs the Lean statement; drop --roadmap-only" >&2; exit 2; }
  case "$MISSION_TYPE" in
    ""|OpenProblem|Textbook|ResearchPaper) ;;
    *) echo "error: --mission-type must be OpenProblem, Textbook, or ResearchPaper" >&2; exit 2 ;;
  esac
fi

for tool in claude uv lake git; do
  command -v "$tool" >/dev/null || { echo "error: '$tool' not found on PATH" >&2; exit 1; }
done

# Tools the headless session may use without prompting.
ALLOWED_TOOLS=(
  "Read" "Edit" "Write" "Glob" "Grep" "Skill"
  "Bash(lake:*)" "Bash(uv run:*)" "Bash(git:*)" "Bash(ls:*)" "Bash(cat:*)"
  "Bash(grep:*)" "Bash(find:*)" "Bash(mkdir:*)"
  "mcp__plugin_autoform_autoform-lsp" "mcp__plugin_autoform_autoform-repl"
)

# `claude -p` exits as soon as Claude ends its reply, killing anything still
# running. A Lean build that imports all of Mathlib can take tens of minutes on
# a small machine, longer than Claude Code's default shell timeout, after which
# the command moves to the background and Claude tends to stop and "wait".
FOREGROUND_NOTE="This session exits the moment you end your reply, and anything
still running is killed with it. Run every command, including long Lean builds
and 'autoform prove2me verify', in the foreground with a timeout of at least
3600000 ms and wait for it to finish. Never run a command in the background and
never end your reply while something is still running."

EXTRA_DIRS=()  # more directories the session may read and edit

run_claude() {  # run_claude <prompt> [extra allowed tools...]
  local prompt="$1"$'\n\n'"$FOREGROUND_NOTE"; shift
  local args=(
    -p "$prompt"
    --plugin-dir "$ROOT"
    --add-dir "$ROOT" ${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}
    --permission-mode acceptEdits
    --allowedTools "${ALLOWED_TOOLS[@]}" "$@"
  )
  [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
  BASH_DEFAULT_TIMEOUT_MS="${BASH_DEFAULT_TIMEOUT_MS:-3600000}" \
    BASH_MAX_TIMEOUT_MS="${BASH_MAX_TIMEOUT_MS:-3600000}" \
    claude "${args[@]}"
}

# --- Prove2Me mission mode ---------------------------------------------------
# The autoform:prove2me skill drives the work and `autoform prove2me` makes
# every API call, so this session never handles the key or needs curl.
if [[ -n "$PROVE2ME" ]]; then
  # Clone the workspace, ask for a key, and pin and fetch the mission's
  # Mathlib, each only if missing. Prints the checkout to work in.
  PROVE2ME_WORKSPACE="$(uv run --quiet --project "$ROOT" autoform prove2me setup \
    --mission "$PROVE2ME" --print-workspace)" || exit 1
  export PROVE2ME_WORKSPACE
  uv run --quiet --project "$ROOT" autoform prove2me auth >/dev/null || exit 1

  git -C "$PROVE2ME_WORKSPACE" pull -q --ff-only \
    || echo "warning: could not update $PROVE2ME_WORKSPACE; continuing with local copy" >&2

  if [[ "$DRY_RUN" -eq 1 ]]; then
    SUBMIT="This is a dry run: take the work as far as passing
'autoform prove2me verify --dry-run' (and 'publish --dry-run' for any sketch
children), then stop. Do not submit, publish, or edit anything on Prove2Me.
Show the solution file, its target theorem id, and the explanation you would
attach."
  else
    SUBMIT="The user asks you to submit: once 'verify --dry-run' passes,
submit with an explanation, publishing sketch children first if you
decompose. Do not comment, vote, rate, accept licensing, or create missions."
  fi

  read -r -d '' PROMPT <<EOF || true
Use the autoform:prove2me skill to contribute to the Prove2Me mission
$PROVE2ME: pick an open frontier theorem and submit a proof or proof sketch.
${STATEMENT:+Hint from the user about what to target: "$STATEMENT"
}
You are running non-interactively, so nobody can answer questions; make
reasonable choices and explain them at the end. The Prove2Me workspace is
$PROVE2ME_WORKSPACE, already pinned to this mission's Mathlib and exported as
PROVE2ME_WORKSPACE, so the CLI uses it by default. Run the CLI as
'uv run --project "$ROOT" autoform prove2me ...'.

$SUBMIT
EOF

  echo "==> Prove2Me mission: $PROVE2ME"
  echo "    workspace: $PROVE2ME_WORKSPACE"
  [[ "$DRY_RUN" -eq 1 ]] && echo "    dry run: nothing will be submitted"
  cd "$PROVE2ME_WORKSPACE"
  run_claude "$PROMPT" "WebFetch" "WebSearch"
  exit $?
fi

# "6 is not a prime number" -> slug "6-is-not-a-prime-number",
# Lean module "Stmt6IsNotAPrimeNumber" (identifiers cannot start with a digit).
SLUG="$(printf '%s' "$STATEMENT" | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-60 | sed -E 's/-+$//')"
[[ -n "$SLUG" ]] || SLUG="statement"
MODULE="$(printf '%s' "$SLUG" | awk -F- '{for (i = 1; i <= NF; i++) printf "%s", toupper(substr($i, 1, 1)) substr($i, 2)}')"
[[ "$MODULE" =~ ^[A-Za-z] ]] || MODULE="Stmt$MODULE"

autoform() { uv run --quiet --project "$ROOT" autoform "$@"; }

# --- Workspace ---------------------------------------------------------------
if [[ ! -d "$PROJECT" ]]; then
  echo "==> Creating Lean + Mathlib workspace at $PROJECT (first run only; this downloads Mathlib)"
  mkdir -p "$PROJECT"
  (cd "$PROJECT" && lake init "$PACKAGE" math)
fi
PROJECT="$(cd "$PROJECT" && pwd)"

# This checkout is loaded with --plugin-dir, and Claude Code treats every file
# under a plugin directory as sensitive: Write and Edit are refused there even
# with acceptEdits, so the session would stop asking to "allow edits".
case "$PROJECT/" in
  "$ROOT/"*)
    echo "error: the workspace $PROJECT is inside this checkout ($ROOT)." >&2
    echo "  Claude Code will not edit files inside its plugin directory, so pick a" >&2
    echo "  workspace elsewhere with --project DIR or AUTOFORM_STATEMENTS_DIR" >&2
    echo "  (the default is ~/autoform_statements). An existing workspace can be moved:" >&2
    echo "    mv \"$PROJECT\" ~/autoform_statements" >&2
    exit 1 ;;
esac

if [[ "$ROADMAP_ONLY" -eq 0 ]]; then
  echo "==> Fetching Mathlib build cache"
  (cd "$PROJECT" && lake exe cache get)
fi

if [[ ! -d "$PROJECT/.git" ]]; then
  git -C "$PROJECT" init -q
fi
if [[ ! -d "$PROJECT/blueprint" ]]; then
  echo "==> Scaffolding blueprint"
  autoform init "$PROJECT" --title "Single statements"
fi
if ! git -C "$PROJECT" rev-parse -q --verify HEAD >/dev/null; then
  git -C "$PROJECT" add -A
  git -C "$PROJECT" commit -q -m "Initial Lean and blueprint scaffold"
fi

# Lean source directory: the package's own directory if lake made one.
LEAN_DIR="$PACKAGE"
[[ -d "$PROJECT/$LEAN_DIR" ]] || LEAN_DIR="$(cd "$PROJECT" && { ls -d */ 2>/dev/null \
  | grep -vE '^(blueprint|\.lake|site|site-src|theme)/$' | grep -E '^[A-Z]' | head -1 | tr -d /; } || true)"
LEAN_FILE="${LEAN_DIR:-$PACKAGE}/$MODULE.lean"

# --- Prompt ------------------------------------------------------------------
AUTOFORM_CMD="uv run --project \"$ROOT\" autoform"
read -r -d '' PROMPT <<EOF || true
You are running AutoformBot non-interactively on a single statement, not a
paper. Nobody is available to answer questions: the scope below is already
confirmed and the one-node roadmap is pre-approved, so do not stop to ask.

Statement (verbatim from the user):
  "$STATEMENT"

Project root: $PROJECT
Autoform CLI: $AUTOFORM_CMD <subcommand> ...

1. Use the autoform:roadmap skill. The source is the statement itself: record
   it as a short project-authored specification in
   blueprint/sources/$SLUG.md, stating your precise mathematical reading of it
   (and any ambiguity you resolved). Write exactly one roadmap article at
   blueprint/roadmap/$SLUG.md with a single main result, a \`declaration:\`
   kind, a proof sketch, and an empty or genuine \`## Depends on\` section.
   Add one row for it to blueprint/coverage/README.md and update the landing
   pages so they no longer say nothing is planned.
EOF

if [[ "$ROADMAP_ONLY" -eq 0 ]]; then
  PROMPT+="
2. Formalize the statement in $LEAN_FILE (module ${LEAN_FILE%.lean}, with
   '/' read as '.') and import it from the package root file so 'lake build'
   compiles it. Prefer Mathlib's existing notions (e.g. Nat.Prime). Prove it
   with no sorry, admit, or new axioms. Use the autoform-lsp / autoform-repl
   MCP tools (pass project_dir=$PROJECT) or 'lake build' to check your work.
   If you cannot finish the proof, leave the statement compiled with sorry and
   say so plainly rather than weakening it.
3. Once 'lake build' succeeds, set \`statement: formalized\`, and
   \`proof: formalized\` only if the proof is complete, plus the
   fully-qualified \`lean:\` name in the article's frontmatter.
"
fi

PROMPT+="
Finally run '$AUTOFORM_CMD check $PROJECT/blueprint --lean-root $PROJECT',
fix anything it reports, and commit the result in $PROJECT (do not push).
Report the Lean statement you wrote, whether it is fully proved, and the paths
you changed."

# --- Mission proposal --------------------------------------------------------
EXTRA_TOOLS=()
if [[ "$PROPOSE" -eq 1 ]]; then
  # Proposals are drafted and built in the platform's default environment,
  # which is usually not the Mathlib this workspace got from 'lake init'.
  PROVE2ME_WORKSPACE="$(autoform prove2me setup --env default --print-workspace)" || exit 1
  export PROVE2ME_WORKSPACE
  # Fail now, not after a long Claude run, if the session could not use the key.
  autoform prove2me auth >/dev/null || exit 1
  EXTRA_DIRS+=("$PROVE2ME_WORKSPACE")
  # Agent runs the blind read-backs; the web tools find the literature.
  EXTRA_TOOLS+=("Agent" "WebFetch" "WebSearch")

  if [[ -n "$MISSION_TYPE" ]]; then
    TYPE_LINE="$MISSION_TYPE, as the user chose. If it does not describe the
     statement (for example OpenProblem for a known result), keep it but say
     so plainly in your report."
  else
    TYPE_LINE="choose the one that fits, as defined in the captain guide:
     OpenProblem for a question that is genuinely unsolved, Textbook for a
     known result or exercise, ResearchPaper for a result from a specific
     paper. Give your reason in the report."
  fi
  MISSION_NAME="a short descriptive name of your choice"
  [[ -z "$MISSION_TITLE" ]] || MISSION_NAME="\"$MISSION_TITLE\""
  if [[ "$DRY_RUN" -eq 1 ]]; then
    UPLOAD="This is a dry run: take the proposal as far as passing
   '$AUTOFORM_CMD prove2me propose prove2me/proposal.json --dry-run', then
   stop. Do not upload anything to Prove2Me."
  else
    UPLOAD="Once the dry run passes, upload it with
   '$AUTOFORM_CMD prove2me propose prove2me/proposal.json'. Do not create
   fields, publish theorems, submit proofs, comment, or vote; submitting the
   proposal is the user's step on the website."
  fi
  PROMPT+="

4. After that commit, draft a Prove2Me mission proposal for this statement with the
   autoform:prove2me skill, following its 'Propose a mission' section.
   - Name: $MISSION_NAME.
   - Type: $TYPE_LINE
     Visibility public (it stays a private draft until the user submits it).
   - Fields: tag it with the established Prove2Me field(s) that fit best,
     found with 'prove2me get /fields --param q=...'. Do not create a field.
   - Before drafting the goal, search for an existing published statement
     ('prove2me get /theorems --param q=...') and use it as a reference item
     if one is faithful.
   - The Prove2Me workspace is $PROVE2ME_WORKSPACE, pinned to the
     platform's default Mathlib and exported as PROVE2ME_WORKSPACE. The goal
     is the statement you formalized above, restated as a draft ending in
     ':= by sorry' that builds there; do not include your proof.
   - Write the spec to prove2me/proposal.json in $PROJECT and commit it.
   $UPLOAD"
  PROMPT+="
   Report the proposal id and status (or the dry-run result), its items and
   milestones, and what the user should check before submitting it."
fi

# --- Run ---------------------------------------------------------------------
echo "==> Running AutoformBot on: $STATEMENT"
echo "    article: $PROJECT/blueprint/roadmap/$SLUG.md"
[[ "$ROADMAP_ONLY" -eq 1 ]] || echo "    lean:    $PROJECT/$LEAN_FILE"
if [[ "$PROPOSE" -eq 1 ]]; then
  echo "    proposal: Prove2Me ${MISSION_TYPE:-mission} draft, built in $PROVE2ME_WORKSPACE"
  [[ "$DRY_RUN" -eq 0 ]] || echo "    dry run: nothing will be uploaded"
fi
cd "$PROJECT"
run_claude "$PROMPT" ${EXTRA_TOOLS[@]+"${EXTRA_TOOLS[@]}"}
