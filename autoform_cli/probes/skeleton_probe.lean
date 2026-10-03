-- Autoform skeleton probe helpers. This file is a Python-format template: `{{`/`}}`
-- are literal braces and single-brace fields are filled by autoform_cli.skeleton.
-- Each extraction compiles it once, with `lake env lean -o` inside the built
-- project, into a module in a temporary directory; it never modifies the
-- project. Each probe imports one root module and this module, and only calls
-- `AutoformSkeleton.main`, so no project name, notation, or option is in scope
-- while these helpers elaborate.
import Lean.Util.CollectAxioms
import Lean.Util.Path
import Lean.Elab.Command
import Lean.Data.Json

open Lean Elab Command Meta Term

namespace AutoformSkeleton

/-- The probe-to-Python contract for elaborated declaration material. Bump this
when the canonical expression encoding below changes. -/
def semanticSchema := "autoform-lean-expr/v4"

/-- Preserve the structure of a Lean name. `Name.toString` is deliberately not
used: quoted components may themselves contain dots. -/
partial def nameJson : Name → Json
  | .anonymous => Json.null
  | .str p s   => Json.mkObj [("str", Json.arr #[nameJson p, Json.str s])]
  | .num p n   => Json.mkObj [("num", Json.arr #[nameJson p, n])]

partial def levelJson (levelParams : List Name) : Level → Json
  | .zero     => Json.mkObj [("zero", Json.null)]
  | .succ u   => Json.mkObj [("succ", levelJson levelParams u)]
  | .max u v  => Json.mkObj [("max", Json.arr #[levelJson levelParams u, levelJson levelParams v])]
  | .imax u v => Json.mkObj [("imax", Json.arr #[levelJson levelParams u, levelJson levelParams v])]
  | .param n  => match levelParams.idxOf? n with
    | some i => Json.mkObj [("param", i)]
    | none   => Json.mkObj [("unknownParam", nameJson n)]
  | .mvar id  => Json.mkObj [("mvar", nameJson id.name)]

def binderInfoJson : BinderInfo → Json
  | .default        => "default"
  | .implicit       => "implicit"
  | .strictImplicit => "strictImplicit"
  | .instImplicit   => "instImplicit"

def literalJson : Literal → Json
  | .natVal n => Json.mkObj [("nat", n)]
  | .strVal s => Json.mkObj [("string", s)]

/-- Canonical kernel material of a leaf expression; `exprPieces` encodes the rest. -/
def leafJson (levelParams : List Name) : Expr → Json
  | .bvar i          => Json.mkObj [("bvar", i)]
  | .fvar id         => Json.mkObj [("fvar", nameJson id.name)]
  | .mvar id         => Json.mkObj [("mvar", nameJson id.name)]
  | .sort u          => Json.mkObj [("sort", levelJson levelParams u)]
  | .const n us      => Json.mkObj [
      ("const", nameJson n), ("levels", Json.arr (us.toArray.map (levelJson levelParams)))]
  | .lit l           => Json.mkObj [("literal", literalJson l)]
  | _                => Json.null

/-- Safety as the environment records it. The kernel face of a `partial def` is a
safe `opaque` whose compiled implementation is the `_unsafe_rec` companion; the
companion alone is not evidence, since ordinary recursive definitions have one. -/
def safetyJson (env : Environment) (c : Name) (info : ConstantInfo) : Json :=
  if info.isPartial then Json.str "partial"
  else if info.isUnsafe then Json.str "unsafe"
  else if (info matches .opaqueInfo _) &&
      (env.find? (Compiler.mkUnsafeRecName c)).any (fun companion => companion.isPartial) then
    Json.str "partial"
  else Json.str "safe"

/-- Serialized material: literal text, or a fragment the probe states once per
run, with the size of its text. Proof terms share subterms heavily, and their
tree serialization would repeat each shared subterm in full. -/
inductive Piece where
  | lit (text : String)
  | ref (id : Nat) (size : Nat)
  deriving BEq, Hashable

def Piece.size : Piece → Nat
  | .lit text => text.utf8ByteSize
  | .ref _ size => size

/-- Expressions whose text is shorter than this stay inline. -/
def fragmentThreshold : Nat := 256

/-- Expression fragments, expansions, and emitted shared entries, kept across
roots in one probe. The environment is immutable for the generated `run_cmd`,
so `Name` is a complete expansion key. A fragment is keyed by its own pieces,
which fix its text, so equal subterms share one fragment wherever they occur. -/
structure SemanticCache where
  fragmentIds : Std.HashMap (Array Piece) Piece := {{}}
  expanded : Std.HashMap Name (Array Name × Array Name) := {{}}
  emitted : Std.HashSet (String × Name) := {{}}
  outputBytes : Nat := 0
  output : IO.FS.Handle
  /-- Set once a record could not be written, which ends the probe: a later
  record could name a fragment that never reached the file. -/
  broken : Bool := false

def probeOutputLimit : Nat := {output_limit}
/-- Characters of an error message the probe reports for one root. -/
def errorLimit : Nat := {error_limit}

/-- Write one complete record without letting the scratch file grow past the
CLI's output limit. The Python reader checks the limit again after exit. -/
def emitRecord (cache : IO.Ref SemanticCache) (record : Json) : IO Unit := do
  let line := s!"{marker}{{record.compress}}\n"
  let total := (← cache.get).outputBytes + line.utf8ByteSize
  if total > probeOutputLimit then
    cache.modify fun c => {{ c with broken := true }}
    throw <| IO.userError s!"lake env lean exceeded the {{probeOutputLimit}}-byte output limit"
  cache.modify fun c => {{ c with outputBytes := total }}
  try
    (← cache.get).output.putStr line
  catch e =>
    cache.modify fun c => {{ c with broken := true }}
    throw e

/-- Pieces as the probe prints them: adjacent text merged into one string and
each fragment as its number. Expanding the numbers gives `Json.compress`. -/
def piecesJson (parts : Array Piece) : Json := Id.run do
  let mut out : Array Json := #[]
  let mut text := ""
  for part in parts do
    match part with
    | .lit s => text := text ++ s
    | .ref id _ =>
      unless text.isEmpty do
        out := out.push (Json.str text)
      text := ""
      out := out.push (toJson id)
  unless text.isEmpty do
    out := out.push (Json.str text)
  return Json.arr out

/-- Inline short text; state longer text once as a numbered fragment, which is
emitted after the fragments it refers to. -/
def sealPieces (cache : IO.Ref SemanticCache) (parts : Array Piece) : IO Piece := do
  let size := parts.foldl (fun n part => n + part.size) 0
  if size < fragmentThreshold then
    -- Every part is shorter than the whole, so every part is literal.
    return .lit (parts.foldl (fun text part => match part with
      | .lit s => text ++ s
      | .ref .. => text) "")
  if let some piece := (← cache.get).fragmentIds[parts]? then
    return piece
  let id := (← cache.get).fragmentIds.size
  cache.modify fun c => {{ c with fragmentIds := c.fragmentIds.insert parts (.ref id size) }}
  let entry := Json.mkObj [
    ("table", Json.str "fragment"), ("name", Json.str (toString id)), ("value", piecesJson parts)]
  emitRecord cache entry
  return .ref id size

/-- Canonical kernel expression material, as pieces. Binder display names and
metadata do not affect meaning, so they are omitted. Applications and implicit
arguments remain explicit, which exposes macro expansions and synthesized
instances. `Json.compress` is compositional, so each node's text is its syntax
around its children's text. -/
partial def exprPieces (cache : IO.Ref SemanticCache) (lp : List Name) (e : Expr) : IO Piece := do
  if let .mdata _ b := e then
    return ← exprPieces cache lp b
  let go := exprPieces cache lp
  let text (j : Json) : Piece := .lit j.compress
  let parts : Array Piece ← match e with
    | .app f a => do
      pure #[.lit "{{\"app\":[", ← go f, .lit ",", ← go a, .lit "]}}"]
    | .lam _ t b bi => do
      pure #[.lit "{{\"lam\":[", text (binderInfoJson bi), .lit ",", ← go t, .lit ",", ← go b,
        .lit "]}}"]
    | .forallE _ t b bi => do
      pure #[.lit "{{\"forall\":[", text (binderInfoJson bi), .lit ",", ← go t, .lit ",", ← go b,
        .lit "]}}"]
    | .letE _ t v b nd => do
      pure #[.lit "{{\"let\":[", text (Json.bool nd), .lit ",", ← go t, .lit ",", ← go v,
        .lit ",", ← go b, .lit "]}}"]
    | .proj n i b => do
      pure #[.lit "{{\"projection\":[", text (nameJson n), .lit ",", text (i : Json), .lit ",",
        ← go b, .lit "]}}"]
    | _ => pure #[text (leafJson lp e)]
  sealPieces cache parts

/-- Elaboration result whose exact bytes bind a review to kernel-visible
meaning. The theorem proof is excluded; definition and opaque bodies are not.
Object fields appear in `Json.mkObj`'s sorted order. -/
def semanticParts (cache : IO.Ref SemanticCache) (env : Environment) (c : Name) :
    IO (Array Piece) := do
  let expr (lp : List Name) (e : Expr) := exprPieces cache lp e
  let safety (info : ConstantInfo) : Piece := .lit (safetyJson env c info).compress
  match env.find? c with
  | some info@(.defnInfo {{ value, .. }}) | some info@(.opaqueInfo {{ value, .. }}) =>
    return #[.lit "{{\"safety\":", safety info, .lit ",\"type\":", ← expr info.levelParams info.type,
      .lit ",\"value\":", ← expr info.levelParams value, .lit "}}"]
  | some (.inductInfo v) =>
    let mut parts : Array Piece := #[.lit "{{\"constructors\":["]
    for ctor in v.ctors, i in [0:v.ctors.length] do
      parts := parts.push <| .lit <|
        (if i == 0 then "" else ",") ++ "{{\"name\":" ++ (nameJson ctor).compress ++ ",\"type\":"
      parts := parts.push <| ← match env.find? ctor with
        | some info => expr info.levelParams info.type
        | none => pure (.lit "null")
      parts := parts.push (.lit "}}")
    return parts ++ #[.lit "],\"safety\":", safety (.inductInfo v), .lit ",\"type\":",
      ← expr v.levelParams v.type, .lit "}}"]
  | some info =>
    return #[.lit "{{\"safety\":", safety info, .lit ",\"type\":", ← expr info.levelParams info.type,
      .lit "}}"]
  | none => return #[.lit "null"]

/-- Constants that fix the *meaning* of `c`: its type always, and its value only
when `c` is a definition. A theorem's proof is never part of its meaning. A
structure's projections fix which field each name selects, so they belong to
the structure: constructor binder names are not serialized. -/
def meaningConstants (env : Environment) (c : Name) : Array Name :=
  match env.find? c with
  | some (.defnInfo {{ type, value, .. }}) | some (.opaqueInfo {{ type, value, .. }}) =>
    type.getUsedConstants ++ value.getUsedConstants
  | some (.inductInfo v) =>
    v.type.getUsedConstants ++ v.ctors.toArray ++
      ((getStructureInfo? env c).map (·.fieldInfo.map (·.projFn))).getD #[]
  | some info            => info.type.getUsedConstants
  | none                 => #[]

/-- Fold only declarations that Lean's environment proves are generated
companions onto the declaration a reader sees in the source. Name spelling is
not evidence: users may deliberately write names such as `visible._helper`. -/
partial def canonical (env : Environment) (c : Name) : Name :=
  match env.find? c with
  | some (.ctorInfo v) => v.induct
  | some (.recInfo v)  => canonical env v.getMajorInduct
  | _ =>
    if let some info := env.getProjectionFnInfo? c then canonical env info.ctorName
    else if isAuxRecursor env c || isNoConfusion env c || Meta.isMatcherCore env c then
      if c.getPrefix != Name.anonymous && env.contains c.getPrefix then canonical env c.getPrefix else c
    else c

/-- Kernel material for a source declaration and every generated companion
whose implementation contributes to it. Generated names stay out of the human
reading list, but their bodies must remain inside the semantic identity. The
material is printed as pieces whose expansion is its `Json.compress` text. -/
def semanticMaterial (cache : IO.Ref SemanticCache) (env : Environment) (c : Name)
    (generated : Array Name) : IO Json := do
  let mut parts : Array Piece := #[.lit "{{\"generated\":["]
  for d in generated.qsort Name.lt, i in [0:generated.size] do
    parts := parts.push (.lit ((if i == 0 then "" else ",") ++ "{{\"material\":"))
    parts := parts ++ (← semanticParts cache env d)
    parts := parts.push (.lit (",\"name\":" ++ (nameJson d).compress ++ "}}"))
  parts := parts.push (.lit "],\"root\":")
  parts := parts ++ (← semanticParts cache env c)
  return piecesJson (parts.push (.lit "}}"))

/-- The generated companions of a folded declaration, and its direct
meaning-dependencies. Companions are traversed but folded back onto the source
declaration. Results are shared across roots because both the environment and
project roots are fixed for one generated probe. -/
def expandedMeaning (cache : IO.Ref SemanticCache) (env : Environment) (isLocal : Name → Bool) (c : Name) :
    CommandElabM (Array Name × Array Name) := do
  if let some expansion := (← cache.get).expanded[c]? then
    return expansion
  let expansion := Id.run do
    let mut out : Array Name := #[]
    let mut work : Array Name := #[c]
    let mut seen : Array Name := #[]
    while h : work.size > 0 do
      let d := work[work.size - 1]
      work := work.pop
      if seen.contains d then continue
      seen := seen.push d
      for e in meaningConstants env d do
        let f := canonical env e
        if f == c then
          if !seen.contains e then work := work.push e
        else if !isLocal f && (env.getProjectionFnInfo? e).any fun info => info.fromClass then
          continue
        else if !out.contains f then
          out := out.push f
    return (seen.erase c, out)
  cache.modify fun s => {{ s with expanded := s.expanded.insert c expansion }}
  return expansion

def kindOf (env : Environment) (c : Name) : String :=
  match env.find? c with
  | some (.defnInfo _)   => if isInstanceCore env c then "instance" else "def"
  | some (.thmInfo _)    => "theorem"
  | some (.axiomInfo _)  => "axiom"
  | some (.opaqueInfo _) => "opaque"
  | some (.inductInfo _) => if isClass env c then "class" else if isStructure env c then "structure" else "inductive"
  | some (.ctorInfo _)   => "constructor"
  | some (.recInfo _)    => "recursor"
  | some (.quotInfo _)   => "quot"
  | none                 => "unknown"

/-- The options signatures print under, on top of the probe's defaults. -/
def packetOptions (opts : Options) : Options :=
  -- A reader must see what is quantified over: `∃ n : ℕ, …`, not `∃ n, …`,
  -- and where a cast lands: `(↑n : ℚ)`, not `↑n`, since `1 / ↑n` means
  -- something else in `ℕ`.
  (opts.setBool `pp.funBinderTypes true).setBool `pp.coercions.types true

/-- With `raw`, the signature bypasses project notation, unexpanders, and custom
delaborators. Project syntax can print `HMul.hMul a b` as `a + b`; this form
cannot. -/
def signatureOf (c : Name) (raw := false) : CommandElabM String := do
  let sig ← liftTermElabM <|
    withOptions (fun opts => (packetOptions opts).setBool `pp.raw raw) (PrettyPrinter.ppSignature c)
  return sig.fmt.pretty' (← getOptions)

/-- Slice the exact half-open source range recorded by Lean. Positions use
Unicode columns, so convert them through `FileMap` before slicing UTF-8 bytes. -/
def sourceSlice (text : String) (r : DeclarationRange) : String :=
  let fileMap := text.toFileMap
  let startPos := fileMap.ofPosition r.pos
  let endPos := fileMap.ofPosition r.endPos
  String.fromUTF8! (text.toUTF8.extract startPos.byteIdx endPos.byteIdx)

/-! Comment ranges. Lean's parser, not a lexer guess, decides what is a comment:
a project token such as `=--` or `+/-` is code, and `/--/ … -/` is a docstring.
Arithmetic below is spelled `Nat.succ`/`Nat.add`, which no notation can
reinterpret. -/

/-- End of a line comment starting at `i`: the next newline, kept as layout. -/
partial def lineCommentEnd (bytes : ByteArray) (i stop : Nat) : Nat :=
  if Nat.ble stop i || bytes[i]! == 10 then i else lineCommentEnd bytes i.succ stop

/-- End of a block comment whose body starts at `i`, nested `depth` deep. -/
partial def blockCommentEnd (bytes : ByteArray) (i stop depth : Nat) : Nat :=
  if depth == 0 then i
  else if Nat.ble stop i.succ then stop
  else if bytes[i]! == 45 && bytes[i.succ]! == 47 then blockCommentEnd bytes (Nat.add i 2) stop depth.pred
  else if bytes[i]! == 47 && bytes[i.succ]! == 45 then blockCommentEnd bytes (Nat.add i 2) stop depth.succ
  else blockCommentEnd bytes i.succ stop depth

/-- Comments inside `bytes[i:stop]`, a whitespace run Lean attached to a token.
Whitespace holds only blanks, `--` line comments, and `/- -/` block comments. -/
partial def whitespaceComments (bytes : ByteArray) (i stop : Nat) (acc : Array (Nat × Nat)) :
    Array (Nat × Nat) :=
  if Nat.ble stop i.succ then acc
  else if bytes[i]! == 45 && bytes[i.succ]! == 45 then
    let e := lineCommentEnd bytes i stop
    whitespaceComments bytes e stop (acc.push (i, e))
  else if bytes[i]! == 47 && bytes[i.succ]! == 45 then
    let e := blockCommentEnd bytes (Nat.add i 2) stop 1
    whitespaceComments bytes e stop (acc.push (i, e))
  else whitespaceComments bytes i.succ stop acc

def infoComments (bytes : ByteArray) (info : SourceInfo) (acc : Array (Nat × Nat)) :
    Array (Nat × Nat) :=
  match info with
  | .original leading _ trailing _ =>
    let acc := whitespaceComments bytes leading.startPos.byteIdx leading.stopPos.byteIdx acc
    whitespaceComments bytes trailing.startPos.byteIdx trailing.stopPos.byteIdx acc
  | _ => acc

/-- UTF-8 byte ranges of every comment and docstring in `stx`, parsed from `bytes`. -/
partial def syntaxComments (bytes : ByteArray) (stx : Syntax) (acc : Array (Nat × Nat)) :
    Array (Nat × Nat) :=
  match stx with
  | .node _ k args =>
    if k == ``Parser.Command.docComment then
      match stx.getPos?, stx.getTailPos? with
      | some s, some e => infoComments bytes stx.getTailInfo (acc.push (s.byteIdx, e.byteIdx))
      | _, _ => acc
    else args.foldl (fun acc arg => syntaxComments bytes arg acc) acc
  | .atom info _ => infoComments bytes info acc
  | .ident info .. => infoComments bytes info acc
  | .missing => acc

/-- The comment ranges of `stx`, parsed from `bytes`, which hold the source
after `offset` blank columns, relative to the source and cut at `cut`. -/
def commentsJson (bytes : ByteArray) (stx : Syntax) (offset cut : Nat) : Json :=
  let ranges := (syntaxComments bytes stx #[]).foldl (init := #[]) fun acc (s, e) =>
    let r := (s - offset, Nat.min (e - offset) cut)
    if Nat.ble cut r.1 || acc.contains r then acc else acc.push r
  Json.arr <| (ranges.qsort (fun a b => Nat.blt a.1 b.1)).map fun (s, e) => Json.arr #[s, e]

/-! Grammars. Lean parses a declaration with the parser state in effect where
it is written, and that state decides what is a comment: with `++"` a token,
`x ++" -- y "` holds a string; without it, a comment. Lean does not record the
state, so the probe rebuilds, from the final environment, every state Lean
could have had there, up to entries that cannot change how the source parses.
A state under which the source does not parse is then one Lean did not have;
the comment ranges every other state gives must agree. The rule, and what it
leaves unproven, is stated once in autoform_cli/README.md, after "A packet
holds only what a blind auditor may see". -/

/-- A parser extension entry as Lean holds it once loaded. -/
abbrev GrammarEntry := ScopedEnvExtension.Entry Parser.ParserExtension.Entry

instance : Inhabited GrammarEntry := ⟨.global (.kind .anonymous)⟩

def entryOf : GrammarEntry → Parser.ParserExtension.Entry
  | .global e => e
  | .scoped _ e => e

/-- What the environment proves about the parser states of one module. -/
structure ModuleGrammar where
  /-- The state at the module's first line: the builtin grammar and the global
  entries of every module Lean loads to compile it, which under the module
  system is not every module it imports, directly or not; `none` when the
  probe cannot rebuild it. -/
  base : Option Parser.ParserExtension.State
  /-- The scoped entries of those modules, active where their namespace is. -/
  imported : Array (Name × Parser.ParserExtension.Entry)
  /-- The module's own entries, in the order Lean added them. -/
  own : Array GrammarEntry
  /-- For each own entry, a position Lean had reached before adding it, when
  one is known. Nondecreasing, since entries come in the order Lean added them. -/
  after : Array (Option Position)
  /-- The token table of a file, outside the module system, whose only import
  is the module: the builtin tokens and the global tokens of the module and of
  everything it imports, directly or not. -/
  importer : Parser.TokenTable

/-- Each module's entries loaded once, and each module's grammar built once,
per probe. -/
structure GrammarCache where
  loaded : Std.HashMap Name (Array GrammarEntry) := {{}}
  modules : Std.HashMap Name ModuleGrammar := {{}}

/-- Whether `p` comes before `q` in a file. -/
def before (p q : Position) : Bool :=
  p.line < q.line || (p.line == q.line && p.column < q.column)

/-- `entry` as Lean loads it from an `.olean`: a parser entry names the
constant that holds its parser. -/
def loadEntry (categories : Parser.ParserCategories) :
    Parser.ParserExtension.OLeanEntry → ImportM Parser.ParserExtension.Entry
  | .token tk => return .token tk
  | .kind k => return .kind k
  | .category c d b => return .category c d b
  | .parser c d prio => do
    let (leading, p) ← Parser.mkParserOfConstant categories d
    return .parser c d leading p prio

/-- `ParserExtension.addEntryImpl`, failing where it would panic. -/
def addGrammarEntry (s : Parser.ParserExtension.State) :
    Parser.ParserExtension.Entry → Except String Parser.ParserExtension.State
  | .token tk =>
    if tk == "" then .error "invalid empty symbol"
    else if (s.tokens.find? tk).isSome then .ok s
    else .ok {{ s with tokens := s.tokens.insert tk tk }}
  | .kind k => .ok {{ s with kinds := s.kinds.insert k }}
  | .category c d b =>
    .ok (if s.categories.contains c then s
      else {{ s with categories := s.categories.insert c {{ declName := d, behavior := b }} }})
  | .parser c d leading p prio => do
    return {{ s with categories := ← Parser.addParser s.categories c d leading p prio }}

/-- The entries `mod`, at `idx`, added, as Lean loads them. -/
def loadedEntries (cache : IO.Ref GrammarCache) (env : Environment) (mod : Name) (idx : ModuleIdx) :
    CommandElabM (Array GrammarEntry) := do
  if let some known := (← cache.get).loaded.get? mod then return known
  -- Every category is known here; Lean's own loading checks only that a
  -- category a parser names exists, so the parsers are the ones it built.
  let categories := (Parser.parserExtension.getState env).categories
  let ctx : ImportM.Context := {{ env, opts := {{}} }}
  let entries ← (Parser.parserExtension.ext.getModuleEntries env idx).mapM fun e =>
    match e with
    | .global a => return .global (← (loadEntry categories a).run ctx)
    | .scoped ns a => return .scoped ns (← (loadEntry categories a).run ctx)
  cache.modify fun c => {{ c with loaded := c.loaded.insert mod entries }}
  return entries

/-- The modules whose data Lean loads to compile the module at `modIdx`, as
`importModulesCore` decides: every module it imports, directly or not,
unless it is in the module system. Then each module it imports is loaded,
and through a loaded module only what that module imports `public`ly, or
everything when an unbroken chain of `import all` reaches it. -/
def compiledImports (env : Environment) (modIdx : ModuleIdx) : Std.HashSet Name := Id.run do
  let header := env.header.moduleData[modIdx.toNat]!
  let everything := !header.isModule
  -- Each loaded module, with whether `import all` alone reaches it.
  let mut loaded : Std.HashMap Name Bool := {{}}
  let mut work : Array (Name × Bool) := header.imports.map fun i => (i.module, everything || i.importAll)
  while h : work.size > 0 do
    let (m, all) := work[work.size - 1]
    work := work.pop
    if let some known := loaded.get? m then
      if known || !all then continue
    loaded := loaded.insert m all
    let some idx := env.getModuleIdx? m | continue
    for i in env.header.moduleData[idx.toNat]!.imports do
      if i.isExported || all then
        work := work.push (i.module, everything || (all && i.importAll))
  return loaded.fold (init := {{}}) fun set m _ => set.insert m

/-- The parser states `mod` can have had, as far as the environment shows. A
module's entries are kept in the order Lean added them: global and scoped
entries are added only on the main thread, as commands run. A parser entry
naming a parser declared in the module comes after that declaration began,
since the parser must exist. -/
def moduleGrammar (cache : IO.Ref GrammarCache) (semanticCache : IO.Ref SemanticCache) (mod : Name) :
    CommandElabM ModuleGrammar := do
  if let some known := (← cache.get).modules.get? mod then return known
  let env ← getEnv
  let builtin : Parser.ParserExtension.State := {{
    tokens := ← Parser.builtinTokenTable.get, kinds := ← Parser.builtinSyntaxNodeKindSetRef.get,
    categories := ← Parser.builtinParserCategoriesRef.get }}
  let mut grammar : ModuleGrammar :=
    {{ base := none, imported := #[], own := #[], after := #[], importer := builtin.tokens }}
  if let some modIdx := env.getModuleIdx? mod then
    let compiled := compiledImports env modIdx
    let mut closure : Std.HashSet Name := {{}}
    let mut work : Array Name := env.header.moduleData[modIdx.toNat]!.imports.map (·.module)
    while h : work.size > 0 do
      let m := work[work.size - 1]
      work := work.pop
      if closure.contains m then continue
      closure := closure.insert m
      let some idx := env.getModuleIdx? m | continue
      for i in env.header.moduleData[idx.toNat]!.imports do
        work := work.push i.module
    let mut base : Except String Parser.ParserExtension.State := .ok builtin
    let mut imported := #[]
    let mut importer := builtin.tokens
    -- Lean loads a module's imports in the environment's module order, which
    -- lists every module after its imports.
    for i in [0:modIdx.toNat] do
      let m := env.header.moduleNames[i]!
      if !closure.contains m then continue
      if compiled.contains m then
        let some idx := env.getModuleIdx? m | continue
        for e in ← loadedEntries cache env m idx do
          match e with
          | .global entry =>
            if let .token tk := entry then importer := importer.insert tk tk
            base := base.bind (addGrammarEntry · entry)
          | .scoped ns entry => imported := imported.push (ns, entry)
      else
        let some idx := env.getModuleIdx? m | continue
        for e in Parser.parserExtension.ext.getModuleEntries env idx do
          if let .global (.token tk) := e then importer := importer.insert tk tk
    let own ← loadedEntries cache env mod modIdx
    for e in own do
      if let .global (.token tk) := e then importer := importer.insert tk tk
    let mut bound : Array (Option Position) := own.map fun _ => none
    for h : i in [0:own.size] do
      if let .parser _ declName _ _ _ := entryOf own[i] then
        if env.getModuleIdxFor? declName == some modIdx then
          if let some r ← findDeclarationRanges? declName then
            bound := bound.set! i (some r.range.pos)
    let mut after := #[]
    let mut latest : Option Position := none
    for b in bound do
      latest := match latest, b with
        | some p, some q => some (if before p q then q else p)
        | none, b => b
        | l, none => l
      after := after.push latest
    grammar := {{ base := base.toOption, imported, own, after, importer }}
  cache.modify fun c => {{ c with modules := c.modules.insert mod grammar }}
  emitRecord semanticCache <| Json.mkObj [
    ("table", Json.str "grammar"), ("name", Json.str (toString mod)), ("value", toJson grammar.own.size)]
  return grammar

/-- The most parser states a source is parsed under. -/
def grammarLimit : Nat := 256

/-- The token kinds Lean's parser tables index by name rather than by text:
identifiers and literals. -/
def literalKeys : List String := ["ident", "num", "scientific", "str", "char", "name", "fieldIdx", "hygieneInfo"]

/-- Texts one of which a source must contain for `entry`, added to a state
with tokens `base`, to change how Lean parses it; `none` when any source can.
A token matters only where it occurs. A parser indexed by its first tokens
runs only where one of them is read, and a token is read only where it occurs,
unless it names identifiers or literals. -/
def entryKeys (base : Parser.TokenTable) : Parser.ParserExtension.Entry → Option (List String)
  | .token tk => if tk == "" then none else if (base.find? tk).isSome then some [] else some [tk]
  | .kind _ => some []
  | .category .. => none
  | .parser _ _ _ p _ => match p.info.firstTokens with
    | .tokens tks | .optTokens tks =>
      if tks.any (fun tk => tk == "" || literalKeys.contains tk) then none else some tks
    | _ => none

/-- Whether `stx` holds an `open` of a name that may resolve to one of
`namespaces`; inside the source, that `open` activates scoped entries the probe's
parse does not. -/
partial def opensScoped (namespaces : Array Name) (stx : Syntax) : Bool :=
  let ids := if stx.isOfKind ``Parser.Command.openSimple then stx[0].getArgs
    else if stx.isOfKind ``Parser.Command.openScoped then stx[1].getArgs else #[]
  ids.any (fun id =>
      let n := (id.getId.replacePrefix rootNamespace .anonymous).componentsRev
      namespaces.any fun q => n.isPrefixOf q.componentsRev) ||
    stx.getArgs.any (opensScoped namespaces)

/-- A declaration's source as the probe parses it. -/
structure Snippet where
  text : String
  /-- The column the source starts at in its file. -/
  column : Nat
  /-- An environment for each parser state Lean may have parsed the source
  with, up to entries that cannot change how it parses; empty when there are
  too many. -/
  grammars : Array Environment
  /-- The namespaces with scoped entries that can change how it parses. -/
  namespaces : Array Name

/-- Parse `input`, starting at `column`, as one command of the grammar in
`env`: `none` when it does not parse, and an error when it opens one of
`namespaces`, which leaves the parse unknown. -/
def parseCommand (env : Environment) (column : Nat) (namespaces : Array Name) (input : String) :
    Except Unit (Option (Syntax × ByteArray)) :=
  let padded := "".pushn ' ' column ++ input
  let p := Parser.andthenFn Parser.whitespace (Parser.categoryParserFnImpl `command)
  let ictx := Parser.mkInputContext padded "<input>"
  let tokens := (Parser.parserExtension.getState env).tokens
  let s := p.run ictx {{ env, options := {{}} }} tokens {{ Parser.mkParserState padded with pos := ⟨column⟩ }}
  if s.allErrors.isEmpty && ictx.atEnd s.pos then
    let stx := s.stxStack.back
    if opensScoped namespaces stx then .error () else .ok (some (stx, padded.toUTF8))
  else .ok none

/-- The one result every grammar under which a source parses gives: `none`
when it parses under none, `some none` when they disagree or a parse is
unknown. -/
def agreement {{α : Type}} [BEq α] (results : Array (Except Unit (Option α))) : Option (Option α) := Id.run do
  if results.any (· matches .error _) then return some none
  let parsed := results.filterMap fun | .ok r => r | .error _ => none
  let some first := parsed[0]? | return none
  return some (if parsed.all (· == first) then some first else none)

/-- The inputs a statement is parsed from: the source, then each prefix that
ends before a `:=` with `:= sorry` as its value. -/
def statementInputs (snippet : String) : Array String := Id.run do
  let mut inputs := #[snippet]
  let mut written := ""
  for part in (snippet.splitOn ":=").dropLast do
    written := written ++ part
    inputs := inputs.push (written ++ ":= sorry")
    written := written ++ ":="
  return inputs

/-- Capture a declaration from the same source snapshot the probe inspects,
with every parser state it may have been parsed with, given the inputs it
will be parsed from. The surrounding source-tree guard rejects concurrent
edits. Each state is the module's first-line state, the scoped entries of a
set of namespaces, and the module's own entries up to a point, of which
those in a namespace only when it is in the set:
- An own entry comes no earlier than the point its bound proves.
- A namespace is active only where the file names it above the declaration,
  in an `open` or `namespace`.
- An entry matters only if `entryKeys` says the inputs can feel it, so only
  points just after such an entry, and namespaces holding one, are tried. -/
def declarationSnippet (cache : IO.Ref GrammarCache) (semanticCache : IO.Ref SemanticCache) (c : Name)
    (inputs : String → Array String) : CommandElabM (Option Snippet) := do
  let env ← getEnv
  let some r ← findDeclarationRanges? c | return none
  let some idx := env.getModuleIdxFor? c | return none
  let mod := env.header.moduleNames[idx.toNat]!
  let sp ← getSrcSearchPath
  let some path ← sp.findWithExt "lean" mod | return none
  let text ← IO.FS.readFile path
  let snippet := sourceSlice text r.range
  let column := r.range.pos.column
  let g ← moduleGrammar cache semanticCache mod
  let some base := g.base | return some {{ text := snippet, column, grammars := #[], namespaces := #[] }}
  let inputs := inputs snippet
  let matters (e : Parser.ParserExtension.Entry) : Bool :=
    match entryKeys base.tokens e with
    | none => true
    | some keys => keys.any fun k => inputs.any (·.contains k)
  -- Own entries from `hi` on come after the declaration began.
  let hi := (g.after.findIdx? fun a => a.any fun p => !before p r.range.pos).getD g.own.size
  let mut namespaces : Array Name := #[]
  for (ns, e) in g.imported do
    if !namespaces.contains ns && matters e then namespaces := namespaces.push ns
  for e in g.own.extract 0 hi do
    if let .scoped ns entry := e then
      if !namespaces.contains ns && matters entry then namespaces := namespaces.push ns
  let above := String.fromUTF8! (text.toUTF8.extract 0 (text.toFileMap.ofPosition r.range.pos).byteIdx)
  let named := namespaces.filter fun ns => match ns with
    | .str _ s => above.contains s
    | .num _ n => above.contains (toString n)
    | .anonymous => true
  let inScope (A : Array Name) : GrammarEntry → Bool
    | .global _ => true
    | .scoped ns _ => A.contains ns
  let mut cuts : Array Nat := #[0]
  for i in [0:hi] do
    if inScope named g.own[i]! && matters (entryOf g.own[i]!) then cuts := cuts.push (i + 1)
  if grammarLimit < cuts.size * 2 ^ named.size then
    return some {{ text := snippet, column, grammars := #[], namespaces }}
  let mut grammars : Array Environment := #[]
  for bits in List.range (2 ^ named.size) do
    let active := (List.range named.size).foldl (init := #[]) fun A i =>
      if bits.testBit i then A.push named[i]! else A
    let activeScopes := active.foldl NameSet.insert {{}}
    let mut state := g.imported.foldl (init := Except.ok base) fun s (ns, e) =>
      if active.contains ns then s.bind (addGrammarEntry · e) else s
    let mut states := #[]
    for i in [0:hi + 1] do
      if cuts.contains i then
        if let .ok s := state then states := states.push s
      if let some e := g.own[i]? then
        if i < hi && inScope active e then state := state.bind (addGrammarEntry · (entryOf e))
    for s in states do
      grammars := grammars.push <| Parser.parserExtension.ext.modifyState env fun _ =>
        {{ stateStack := [{{ state := s, activeScopes }}], scopedEntries := {{}}, newEntries := [] }}
  return some {{ text := snippet, column, grammars, namespaces }}

/-- A declaration's source and its comment ranges, `null` unless every parser
state under which it parses agrees on them; the caller then decides whether
the source can be shown. -/
def declarationSource (cache : IO.Ref GrammarCache) (semanticCache : IO.Ref SemanticCache) (c : Name) :
    CommandElabM (Option (String × Json)) := do
  let some s ← declarationSnippet cache semanticCache c (#[·]) | return none
  let comments := s.grammars.map fun env =>
    (parseCommand env s.column s.namespaces s.text).map fun parsed =>
      parsed.map fun (stx, bytes) => commentsJson bytes stx s.column s.text.utf8ByteSize
  return some (s.text, (agreement comments).join.getD Json.null)

/-- Some declarations have a source range only through a parent that Lean's
environment structurally identifies, such as a constructor's inductive type.
Show that parent unless it is a theorem or axiom, whose source carries a proof.
An internal-looking name is not provenance: project metaprograms can create it. -/
partial def companionSource (cache : IO.Ref GrammarCache) (semanticCache : IO.Ref SemanticCache) (c : Name) :
    CommandElabM (Option (String × Json)) := do
  let env ← getEnv
  let parent := c.getPrefix
  if (← findDeclarationRanges? c).isSome || !env.contains parent then return none
  if canonical env c == c then return none
  if (← findDeclarationRanges? parent).isNone then
    return ← companionSource cache semanticCache parent
  -- A field's companions (`S.x._default`, `S.p._autoParam`) read best in the
  -- structure that declares the field.
  let shown := canonical env parent
  let kind := kindOf env shown
  if kind == "theorem" || kind == "axiom" then return none
  declarationSource cache semanticCache shown

/-- The node where a parsed declaration's value starts, ending its statement. -/
def valueNode? (stx : Syntax) : Option Syntax :=
  let decl := (stx.find? (·.isOfKind ``Parser.Command.declaration)).getD stx
  (decl.find? (·.isOfKind ``Parser.Command.declValSimple)).orElse fun _ =>
    (decl.find? (·.isOfKind ``Parser.Command.declValEqns)).orElse fun _ =>
      decl.find? (·.isOfKind ``Parser.Command.whereStructInst)

/-- The statement of a declaration that does not parse whole, as when only its
proof uses `local notation`: cut at successive `:=` tokens and parse the prefix
with `:= sorry` as its value. The parsed value's start is the real statement
boundary. A cut inside a structure-style proof therefore recovers its preceding
`where`; one inside the statement (`let x := …`) does not parse as a command. -/
def statementPrefix? (env : Environment) (column : Nat) (namespaces : Array Name) (snippet : String) :
    Except Unit (Option (String × Syntax × ByteArray)) := do
  let mut written := ""
  for part in (snippet.splitOn ":=").dropLast do
    written := written ++ part
    if let some (stx, bytes) ← parseCommand env column namespaces (written ++ ":= sorry") then
      if let some v := valueNode? stx then
        if let some pos := v.getPos? then
          if Nat.ble (pos.byteIdx - column) written.utf8ByteSize then
            let statementBytes := snippet.toUTF8.extract 0 (pos.byteIdx - column)
            return some (String.fromUTF8! statementBytes, stx, bytes)
    written := written ++ ":="
  return none

/-- The declaration's source up to its value: the statement as written, without
the proof, with its comment ranges. Parsed with Lean's own parser rather than
cut by pattern matching, under every parser state the source may have been
parsed with; states that cut the statement differently leave it unknown, and
states that agree on the cut but not on the comments leave its comments
unknown. -/
def statementSource (cache : IO.Ref GrammarCache) (semanticCache : IO.Ref SemanticCache) (root : Name) :
    CommandElabM (Option (String × Json)) := do
  let env ← getEnv
  let some s ← declarationSnippet cache semanticCache root statementInputs | return none
  let snippet := s.text
  let kind := kindOf env root
  -- A type declaration has no value to strip: all of it is the statement.
  let typeDecl := kind == "structure" || kind == "class" || kind == "inductive"
  let shown (written : String) (stx : Syntax) (bytes : ByteArray) : Option (String × Json) :=
    some (written, commentsJson bytes stx s.column written.utf8ByteSize)
  -- One state's statement: `none` when the source does not parse under it,
  -- `some none` when it parses with no statement to cut.
  let statement (penv : Environment) : Except Unit (Option (Option (String × Json))) := do
    match ← parseCommand penv s.column s.namespaces snippet with
    | none =>
      if typeDecl then return none
      return (← statementPrefix? penv s.column s.namespaces snippet).map fun (written, stx, bytes) =>
        shown written.trimAsciiEnd.toString stx bytes
    | some (stx, bytes) =>
      if typeDecl then return some (shown snippet.trimAsciiEnd.toString stx bytes)
      match valueNode? stx with
      | none =>
        return some (if kind == "axiom" || kind == "opaque" then shown snippet.trimAsciiEnd.toString stx bytes
          else none)
      | some v => match v.getPos? with
        | none => return some none
        -- `pos` is a byte position: cut by bytes, not by characters, or every
        -- `∀` before the value pushes the cut past it.
        | some pos =>
          return some (shown (String.fromUTF8! (snippet.toUTF8.extract 0 (pos.byteIdx - s.column))).trimAsciiEnd.toString
            stx bytes)
  let results := s.grammars.map statement
  match agreement results with
  | some (some found) => return found
  | some none =>
    let written := results.map (·.map (·.map (·.map (·.1))))
    return match agreement written with
      | some (some (some w)) => some (w, Json.null)
      | _ => none
  | none => return if typeDecl then some (snippet.trimAsciiEnd.toString, Json.null) else none

def rangeJson (c : Name) : CommandElabM Json := do
  match ← findDeclarationRanges? c with
  | some r => return Json.arr #[r.range.pos.line, r.range.endPos.line]
  | none   => return Json.null

def emit (cache : IO.Ref SemanticCache) (request : String)
    (fields : List (String × Json)) : CommandElabM Unit :=
  emitRecord cache <| Json.mkObj (("root", Json.str request) :: fields)

/-- Emit one entry of a table shared by every root, the first time a root
needs it. Roots name their trusted declarations, external semantic material,
and boundary modules, so what many roots share is stated once per run. An
entry counts as stated once it is written: when computing it fails, the next
root that needs it tries again. -/
def emitShared (cache : IO.Ref SemanticCache) (table : String) (name : Name)
    (value : CommandElabM Json) : CommandElabM Unit := do
  unless (← cache.get).emitted.contains (table, name) do
    let entry := Json.mkObj [
      ("table", Json.str table), ("name", Json.str (toString name)), ("value", ← value)]
    emitRecord cache entry
    cache.modify fun s => {{ s with emitted := s.emitted.insert (table, name) }}

/-- The modules that belong to the running toolchain. A name root is not
enough: a dependency may name its own module `Lake.Foo`, and that module is
external like any other. A module is core only when its root is a toolchain
library and its `.olean` resolves under the toolchain's own `lib/lean`, the
directory Lean itself seeds the search path with. -/
def toolchainModules (env : Environment) : IO (Std.HashSet Name) := do
  let libDir := (← IO.FS.realPath (← getLibDir (← getBuildDir))).components
  let mut core : Std.HashSet Name := {{}}
  for m in env.header.moduleNames do
    if [{core_roots}].contains m.getRoot then
      let olean ← try IO.FS.realPath (← findOLean m) catch _ => pure (System.FilePath.mk "")
      if libDir.isPrefixOf olean.components then core := core.insert m
  return core

def skeleton
    (projectRoots : List Name)
    (coreModules : Std.HashSet Name)
    (grammarCache : IO.Ref GrammarCache)
    (semanticCache : IO.Ref SemanticCache)
    (request : String) (root : Name) : CommandElabM Unit := do
  let env ← getEnv
  unless env.contains root do
    emit semanticCache request [("found", Json.bool false)]
    return
  -- `env.header` is slow to reach from the interpreted probe, so the module of
  -- each dependency is looked up in names read once per root.
  let moduleNames := env.header.moduleNames
  let moduleOf (n : Name) : Option Name :=
    (env.getModuleIdxFor? n).map fun idx => moduleNames[idx.toNat]!
  let isLocal (n : Name) : Bool :=
    match moduleOf n with
    | some m => projectRoots.any (fun projectRoot => projectRoot.isPrefixOf m)
    | none   => false
  let isCore (n : Name) : Bool :=
    match moduleOf n with
    | some m => coreModules.contains m
    | none   => true
  let expand (c : Name) : CommandElabM (Array Name) :=
    return (← expandedMeaning semanticCache env isLocal c).2
  let material (c : Name) : CommandElabM Json := do
    semanticMaterial semanticCache env c (← expandedMeaning semanticCache env isLocal c).1
  -- Signatures print with the token table of a file whose only import is the
  -- root's module, so a name is escaped as `«…»` exactly where `#check` there
  -- escapes it, whatever the helper's own imports declare.
  let rootGrammar ← moduleGrammar grammarCache semanticCache ((moduleOf root).getD Name.anonymous)
  let printEnv := Parser.parserExtension.modifyState env fun s =>
    {{ s with tokens := rootGrammar.importer }}
  let mut trusted : Array Name := #[]
  let mut edges : Array (Name × Array Name) := #[]
  let mut assumed : Array Name := #[]
  let axioms ← collectAxioms root
  -- Axioms are part of the trust boundary even when reached only through a
  -- theorem proof. Their types can name project definitions or external
  -- notions whose meaning must be bound just like statement dependencies.
  let mut work : Array Name := axioms.push root
  while h : work.size > 0 do
    let c := work[work.size - 1]
    work := work.pop
    let mut localDeps : Array Name := #[]
    for d in ← expand c do
      if isLocal d then
        if !localDeps.contains d then localDeps := localDeps.push d
        if !trusted.contains d && d != root then
          trusted := trusted.push d
          work := work.push d
      else if !isCore d && !assumed.contains d then
        assumed := assumed.push d
    edges := edges.push (c, localDeps.qsort Name.lt)
  let sortedAssumed := assumed.qsort Name.lt
  let sortedAxioms := axioms.qsort Name.lt
  let mut boundaryClosure := sortedAssumed
  -- Membership beside the array: a closure can hold thousands of constants.
  let mut inBoundaryClosure : Std.HashSet Name := Std.HashSet.ofArray sortedAssumed
  for axiomName in sortedAxioms do
    if !isCore axiomName && !inBoundaryClosure.contains axiomName then
      boundaryClosure := boundaryClosure.push axiomName
      inBoundaryClosure := inBoundaryClosure.insert axiomName
  let mut boundaryWork := boundaryClosure
  while h : boundaryWork.size > 0 do
    let c := boundaryWork[boundaryWork.size - 1]
    boundaryWork := boundaryWork.pop
    for d in ← expand c do
      if !isLocal d && !isCore d && !inBoundaryClosure.contains d then
        boundaryClosure := boundaryClosure.push d
        inBoundaryClosure := inBoundaryClosure.insert d
        boundaryWork := boundaryWork.push d
  let sortedBoundaryClosure := boundaryClosure.qsort Name.lt
  let mut boundaryModules : Array Name := #[]
  for c in sortedBoundaryClosure do
    if let some mod := moduleOf c then
      if !boundaryModules.contains mod then boundaryModules := boundaryModules.push mod
  let mut boundaryModuleNames : Array Json := #[]
  -- Compiled artifacts, not source bytes, identify a boundary module: macros,
  -- options, and instances from outside its source change its elaborated
  -- meaning. Lean serializes module names rather than checkout paths, so the
  -- bytes are path-independent for ordinary code. Module-system builds split
  -- the artifact into `.olean`, `.olean.server`, and `.olean.private` (which
  -- holds private bodies and proofs); bind every part that exists.
  for mod in boundaryModules.qsort Name.lt do
    emitShared semanticCache "module" mod do
      let olean ← findOLean mod
      unless ← olean.pathExists do
        throwError "compiled artifact unavailable for boundary module {{mod}}"
      let mut files : Array Json := #[]
      for (kind, path) in [("olean", olean), ("olean.server", olean.addExtension "server"),
          ("olean.private", olean.addExtension "private")] do
        if kind == "olean" || (← path.pathExists) then
          files := files.push <| Json.arr #[Json.str kind, Json.str path.toString]
      return Json.arr files
    boundaryModuleNames := boundaryModuleNames.push (Json.str (toString mod))
  let mut items : Array Json := #[]
  -- Fields shared by root and trusted records; only a trusted one may borrow a parent's source.
  let fields (c : Name) (companion : Bool) : CommandElabM (List (String × Json)) := do
    let kind := kindOf env c
    let shown ← if kind == "theorem" || kind == "axiom" then pure none else do
      let s ← declarationSource grammarCache semanticCache c
      if s.isNone && companion then companionSource grammarCache semanticCache c else pure s
    let deps := (edges.find? (·.1 == c)).map (·.2) |>.getD #[]
    return [
      ("kind", Json.str kind),
      ("module", Json.str (toString ((moduleOf c).getD Name.anonymous))),
      ("range", ← rangeJson c),
      ("signature", Json.str (← withEnv printEnv (signatureOf c))),
      ("raw_signature", Json.str (← withEnv printEnv (signatureOf c (raw := true)))),
      ("semantic_schema", Json.str semanticSchema),
      ("semantic", ← material c),
      ("depends", Json.arr (deps.map fun d => Json.str (toString d))),
      ("source", (shown.map (Json.str ·.1)).getD Json.null),
      ("source_comments", (shown.map (·.2)).getD Json.null)]
  -- A trusted declaration's record does not depend on the root: its
  -- dependencies are its own meaning's local constants.
  for c in trusted.qsort Name.lt do
    emitShared semanticCache "trusted" c do
      return Json.mkObj <| [("name", Json.str (toString c)),
        ("source_name", Json.str (toString (privateToUserName c)))] ++ (← fields c true)
    items := items.push (Json.str (toString c))
  let (statement, statementComments) := match ← statementSource grammarCache semanticCache root with
    | some (s, comments) => (Json.str s, comments)
    | none => (Json.null, Json.null)
  for d in sortedAssumed ++ sortedAxioms do
    emitShared semanticCache "semantic" d (material d)
  emit semanticCache request <| [
    ("found", Json.bool true),
    ("statement_source", statement),
    ("statement_comments", statementComments),
    ("lean_version", Json.str Lean.versionString),
    ("trusted", Json.arr items),
    ("assumed", Json.arr (sortedAssumed.map fun d => Json.str (toString d))),
    ("boundary_modules", Json.arr boundaryModuleNames),
    ("axioms", Json.arr (sortedAxioms.map fun d => Json.str (toString d)))] ++ (← fields root false)

/-- The probe's entry point: the skeleton of each requested root, keyed by the
name as the CLI spelled it. It runs in the probe's command scope, which has no
`open` and no option set, so signatures print with only `packetOptions` added. -/
def main (projectRoots : List Name) (roots : List (String × Name)) : CommandElabM Unit :=
  -- Elaborated proofs can be large; reading them has no heartbeat budget.
  withScope (fun scope => {{ scope with opts := maxHeartbeats.set scope.opts 0 }}) do
  let grammarCache : IO.Ref AutoformSkeleton.GrammarCache ← IO.mkRef {{}}
  let coreModules ← AutoformSkeleton.toolchainModules (← getEnv)
  -- A command's `IO.println` output is captured and printed as one message when
  -- the command ends, at a cost quadratic in its size: on a Mathlib project that
  -- outlasts the probe itself. Write records to the file the CLI names instead,
  -- without redirecting incidental stdout into that trusted record stream.
  let some path ← IO.getEnv "{output_env}" | throwError "{output_env} is not set"
  let out ← IO.FS.Handle.mk path .write
  let semanticCache : IO.Ref AutoformSkeleton.SemanticCache ← IO.mkRef {{ output := out }}
  try
    for (request, root) in roots do
      -- An error confined to one root leaves the others to be read. A record
      -- that could not be written, or an interrupt, ends the whole probe.
      tryCatch (AutoformSkeleton.skeleton projectRoots coreModules grammarCache semanticCache request root)
        fun e => do
          if (← semanticCache.get).broken || e.isInterrupt then throw e
          let message ← e.toMessageData.toString
          emit semanticCache request [("error", Json.str (message.take errorLimit).toString)]
  finally
    out.flush

end AutoformSkeleton

-- Each probe finds this module after the project's own search path, so a
-- project or dependency module of the same name would be imported instead.
run_cmd do
  let helper := Name.mkSimple "{helper_module}"
  if let some path ← (← searchPathRef.get).findWithExt "olean" helper then
    throwError "the project's search path already provides a module named {{helper}}, at {{path}}; \
      the skeleton probe needs that module name for its own helpers"
