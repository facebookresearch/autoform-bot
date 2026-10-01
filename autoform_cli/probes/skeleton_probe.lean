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

def levelParamIndex? : List Name → Name → Option Nat
  | [], _ => none
  | p :: ps, n => if p == n then some 0 else (levelParamIndex? ps n).map Nat.succ

partial def levelJson (levelParams : List Name) : Level → Json
  | .zero     => Json.mkObj [("zero", Json.null)]
  | .succ u   => Json.mkObj [("succ", levelJson levelParams u)]
  | .max u v  => Json.mkObj [("max", Json.arr #[levelJson levelParams u, levelJson levelParams v])]
  | .imax u v => Json.mkObj [("imax", Json.arr #[levelJson levelParams u, levelJson levelParams v])]
  | .param n  => match levelParamIndex? levelParams n with
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

/-- Canonical kernel expression material. Binder display names and metadata do
not affect meaning, so they are omitted. Applications and implicit arguments
remain explicit, which exposes macro expansions and synthesized instances. -/
partial def exprJson (levelParams : List Name) : Expr → Json
  | .bvar i          => Json.mkObj [("bvar", i)]
  | .fvar id         => Json.mkObj [("fvar", nameJson id.name)]
  | .mvar id         => Json.mkObj [("mvar", nameJson id.name)]
  | .sort u          => Json.mkObj [("sort", levelJson levelParams u)]
  | .const n us      => Json.mkObj [
      ("const", nameJson n), ("levels", Json.arr (us.toArray.map (levelJson levelParams)))]
  | .app f a         => Json.mkObj [("app", Json.arr #[exprJson levelParams f, exprJson levelParams a])]
  | .lam _ t b bi    => Json.mkObj [
      ("lam", Json.arr #[binderInfoJson bi, exprJson levelParams t, exprJson levelParams b])]
  | .forallE _ t b bi => Json.mkObj [
      ("forall", Json.arr #[binderInfoJson bi, exprJson levelParams t, exprJson levelParams b])]
  | .letE _ t v b nd => Json.mkObj [
      ("let", Json.arr #[
        Json.bool nd, exprJson levelParams t, exprJson levelParams v, exprJson levelParams b])]
  | .lit l           => Json.mkObj [("literal", literalJson l)]
  | .mdata _ e       => exprJson levelParams e
  | .proj n i e      => Json.mkObj [
      ("projection", Json.arr #[nameJson n, i, exprJson levelParams e])]

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

/-- Serialized material and expression fragments, shared across roots in one
probe. The environment is immutable for the generated `run_cmd`, so `Name` is a
complete material key. A fragment is keyed by its own pieces, which fix its
text, so equal subterms share one fragment wherever they occur. -/
structure SemanticCache where
  materials : Std.HashMap Name Json := {{}}
  fragmentIds : Std.HashMap (Array Piece) Piece := {{}}
  fragments : Nat := 0
  outputBytes : Nat := 0
  output : Option IO.FS.Handle := none

def probeOutputLimit : Nat := {output_limit}

/-- Write one complete record without letting the scratch file grow past the
CLI's output limit. The Python reader checks the limit again after exit. -/
def emitRecord (cache : IO.Ref SemanticCache) (record : Json) : IO Unit := do
  let line := s!"{marker}{{record.compress}}\n"
  let total := (← cache.get).outputBytes + line.utf8ByteSize
  if total > probeOutputLimit then
    throw <| IO.userError s!"lake env lean exceeded the {{probeOutputLimit}}-byte output limit"
  cache.modify fun c => {{ c with outputBytes := total }}
  match (← cache.get).output with
  | some out => out.putStr line
  | none => IO.print line

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
  let id := (← cache.get).fragments
  cache.modify fun c =>
    {{ c with fragments := id + 1, fragmentIds := c.fragmentIds.insert parts (.ref id size) }}
  let entry := Json.mkObj [
    ("table", Json.str "fragment"), ("name", Json.str (toString id)), ("value", piecesJson parts)]
  emitRecord cache entry
  return .ref id size

/-- `exprJson`, as pieces. `Json.compress` is compositional, so each node's
text is its syntax around its children's text. -/
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
    | _ => pure #[text (exprJson lp e)]
  sealPieces cache parts

/-- Elaboration result whose exact bytes bind a review to kernel-visible
meaning. The theorem proof is excluded; definition and opaque bodies are not.
Object fields appear in `Json.mkObj`'s sorted order. -/
def semanticParts (cache : IO.Ref SemanticCache) (env : Environment) (c : Name) :
    IO (Array Piece) := do
  let expr (lp : List Name) (e : Expr) := exprPieces cache lp e
  let safety (info : ConstantInfo) : Piece := .lit (safetyJson env c info).compress
  match env.find? c with
  | some info@(.defnInfo v) =>
    return #[.lit "{{\"safety\":", safety info, .lit ",\"type\":", ← expr v.levelParams v.type,
      .lit ",\"value\":", ← expr v.levelParams v.value, .lit "}}"]
  | some info@(.opaqueInfo v) =>
    return #[.lit "{{\"safety\":", safety info, .lit ",\"type\":", ← expr v.levelParams v.type,
      .lit ",\"value\":", ← expr v.levelParams v.value, .lit "}}"]
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
  | some (.defnInfo v)   => v.type.getUsedConstants ++ v.value.getUsedConstants
  | some (.opaqueInfo v) => v.type.getUsedConstants ++ v.value.getUsedConstants
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
def semanticMaterial (cache : IO.Ref SemanticCache) (env : Environment) (c : Name) : IO Json := do
  let mut generated : Array Name := #[]
  let mut work : Array Name := #[c]
  let mut seen : Array Name := #[]
  while h : work.size > 0 do
    let d := work[work.size - 1]
    work := work.pop
    if seen.contains d then continue
    seen := seen.push d
    for e in meaningConstants env d do
      if e != c && canonical env e == c && !generated.contains e then
        generated := generated.push e
        work := work.push e
  let mut parts : Array Piece := #[.lit "{{\"generated\":["]
  for d in generated.qsort Name.lt, i in [0:generated.size] do
    parts := parts.push (.lit ((if i == 0 then "" else ",") ++ "{{\"material\":"))
    parts := parts ++ (← semanticParts cache env d)
    parts := parts.push (.lit (",\"name\":" ++ (nameJson d).compress ++ "}}"))
  parts := parts.push (.lit "],\"root\":")
  parts := parts ++ (← semanticParts cache env c)
  return piecesJson (parts.push (.lit "}}"))

def cachedSemanticMaterial
    (cache : IO.Ref SemanticCache) (env : Environment) (c : Name) : CommandElabM Json := do
  if let some material := (← cache.get).materials[c]? then
    return material
  let material ← semanticMaterial cache env c
  cache.modify fun s => {{ s with materials := s.materials.insert c material }}
  return material

/-- Direct meaning-dependencies of a folded declaration. Generated companions
are traversed but folded back onto the source declaration. Results are shared
across roots because both the environment and project roots are fixed for one
generated probe. -/
def expandedMeaning
    (cache : IO.Ref (Std.HashMap Name (Array Name)))
    (env : Environment) (projectRoots : List Name) (c : Name) : CommandElabM (Array Name) := do
  if let some dependencies := (← cache.get)[c]? then
    return dependencies
  -- `env.header` is slow to reach from the interpreted probe: read it once.
  let moduleNames := env.header.moduleNames
  let isLocal (n : Name) : Bool :=
    match env.getModuleIdxFor? n with
    | some idx =>
      let mod := moduleNames[idx.toNat]!
      projectRoots.any (fun projectRoot => projectRoot.isPrefixOf mod)
    | none   => false
  let isClassProjection (e : Name) : Bool :=
    match env.getProjectionFnInfo? e with
    | some info => info.fromClass
    | none      => false
  let dependencies := Id.run do
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
        else if !isLocal f && isClassProjection e then
          continue
        else if !out.contains f then
          out := out.push f
    return out
  cache.modify (·.insert c dependencies)
  return dependencies

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

def moduleOf (env : Environment) (c : Name) : Option Name :=
  (env.getModuleIdxFor? c).map fun idx => env.header.moduleNames[idx.toNat]!

/-- The options signatures print under, on top of the probe's defaults. -/
def packetOptions (opts : Options) : Options :=
  -- A reader must see what is quantified over: `∃ n : ℕ, …`, not `∃ n, …`,
  -- and where a cast lands: `(↑n : ℚ)`, not `↑n`, since `1 / ↑n` means
  -- something else in `ℕ`.
  (opts.setBool `pp.funBinderTypes true).setBool `pp.coercions.types true

def signatureOf (c : Name) : CommandElabM String := do
  let sig ← liftTermElabM <| withOptions packetOptions (PrettyPrinter.ppSignature c)
  return sig.fmt.pretty 100

/-- The raw signature, bypassing project notation, unexpanders, and custom
delaborators. Project syntax can print `HMul.hMul a b` as `a + b`; this form
cannot. -/
def rawSignatureOf (c : Name) : CommandElabM String := do
  let sig ← liftTermElabM <|
    withOptions (fun opts => (packetOptions opts).setBool `pp.raw true) (PrettyPrinter.ppSignature c)
  return sig.fmt.pretty 100

/-- First node of syntax kind `k` inside `stx`, depth-first. -/
partial def findKind? (stx : Syntax) (k : SyntaxNodeKind) : Option Syntax :=
  if stx.getKind == k then some stx else stx.getArgs.findSome? (findKind? · k)

/-- The namespace names in the words of one `open` line, and whether the
command can go on: its names continue on more-indented lines, as in
`open A B` followed by `  C`, until `in`, `hiding`, `renaming` or `(`. -/
def openWords (words : String) : List Name × Bool :=
  let ws := (words.splitOn " ").filter (fun t => t ≠ "" && t ≠ "scoped")
  let stops (t : String) := t == "in" || t == "hiding" || t == "renaming" ||
    t.startsWith "(" || t.startsWith "--" || t.startsWith "/-"
  let names := ws.takeWhile (!stops ·)
  (names.map String.toName, names.length == ws.length)

/-- The namespaces the file opens above `pos`, from its `open …` commands,
including a same-line `open … in` prefix and names on continuation lines. Their
scoped notation (`#s`, `n !`, `∑ x ∈ s, f x`) must be active for the statement
to parse; Lean records what a declaration means, not how its file was set up.
Names are returned as written; the caller resolves them against the enclosing
namespaces. -/
def openedNamespaces (lines : List String) (pos : Position) : List Name := Id.run do
  let current := ((lines[pos.line - 1]?.getD "").take pos.column).toString
  let mut names : List Name := []
  -- The column of an `open` whose names may continue on the next line.
  let mut openColumn : Option Nat := none
  for l in lines.take (pos.line - 1) ++ [current] do
    let body := l.trimAsciiStart.toString
    let column := l.length - body.length
    if body.startsWith "open " then
      let (opened, more) := openWords (body.drop 5).toString
      names := names ++ opened
      openColumn := if more then some column else none
    else if let some c := openColumn then
      if body.isEmpty then continue
      if Nat.blt c column then
        let (opened, more) := openWords body
        names := names ++ opened
        if !more then openColumn := none
      else openColumn := none
  return names

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

/-- Comment ranges below byte `cut`, deduplicated and sorted, as JSON pairs. -/
def commentsJson (bytes : ByteArray) (stx : Syntax) (cut : Nat) : Json :=
  let ranges := (syntaxComments bytes stx #[]).foldl (init := #[]) fun acc (s, e) =>
    let r := (s, Nat.min e cut)
    if Nat.ble cut s || acc.contains r then acc else acc.push r
  Json.arr <| (ranges.qsort (fun a b => Nat.blt a.1 b.1)).map fun (s, e) => Json.arr #[s, e]

/-- The global tokens declared by imports of `mod`. Tokens declared in `mod`
itself are not safe here: the final environment does not record whether their
declaration came before or after the source being inspected. Nor does it record
where scoped tokens were opened. A `local` token is not recorded at all. -/
def importedGlobalTokens (env : Environment) (mod : Name) : Std.HashSet String := Id.run do
  let mut tokens : Std.HashSet String := {{}}
  let mut seen : Std.HashSet Name := {{}}
  let some rootIdx := env.getModuleIdx? mod | return tokens
  let mut work : Array Name := env.header.moduleData[rootIdx.toNat]!.imports.map (·.module)
  while h : work.size > 0 do
    let m := work[work.size - 1]
    work := work.pop
    if seen.contains m then continue
    seen := seen.insert m
    let some idx := env.getModuleIdx? m | continue
    for e in Parser.parserExtension.ext.getModuleEntries env idx do
      match e with
      | .global (.token t) => tokens := tokens.insert t
      | _ => pure ()
    for i in env.header.moduleData[idx.toNat]!.imports do
      work := work.push i.module
  return tokens

/-- Whether `text` holds a non-builtin token containing `--` or a block-comment
opener that was not globally active through an import. The root module the
probe imports, which may import more than this declaration's own file, a later
declaration, or a reconstructed scoped `open` may activate it here even when
the source did not, so the probe then cannot safely distinguish that token from
a comment. -/
def commentLikeToken (penv : Environment) (mod : Name) (text : String) : IO Bool := do
  let builtin ← Parser.builtinTokenTable.get
  let holds (s t : String) := Nat.blt 1 (s.splitOn t).length
  let found := ((Parser.getTokenTable penv).findPrefix "").filter fun t =>
    (builtin.find? t).isNone && (holds t "--" || holds t "/-") && holds text t
  if found.isEmpty then return false
  let global := importedGlobalTokens penv mod
  return found.any fun t => !global.contains t

/-- Capture a declaration from the same source snapshot the probe inspects,
and parse it with Lean's own parser. The surrounding source-tree guard rejects
concurrent edits. The parse is `none` when the slice does not parse alone; the
environment it was parsed in is returned for parsing parts of it. -/
def declarationSnippet (c : Name) :
    CommandElabM (Option (String × Option Syntax × Environment)) := do
  let env ← getEnv
  let some r ← findDeclarationRanges? c | return none
  let some idx := env.getModuleIdxFor? c | return none
  let mod := env.header.moduleNames[idx.toNat]!
  let sp ← getSrcSearchPath
  let some path ← sp.findWithExt "lean" mod | return none
  let text ← IO.FS.readFile path
  let lines := text.splitOn "\n"
  let snippet := sourceSlice text r.range
  -- The declaration sits inside the namespaces its name lives in, and `open X`
  -- written there may refer to any of them, as in `namespace A` … `open B`.
  let mut scopes : Array Name := #[]
  let mut ns := (privateToUserName c).getPrefix
  while ns != Name.anonymous do
    scopes := scopes.push ns
    ns := ns.getPrefix
  -- `activateScoped` mutates the environment. Isolate those parser-only changes
  -- so one requested declaration cannot change how the next one is parsed.
  withEnv env do
    for scope in scopes do
      if env.isNamespace scope then activateScoped scope
    for opened in openedNamespaces lines r.range.pos do
      for scope in scopes.push Name.anonymous do
        if env.isNamespace (scope ++ opened) then activateScoped (scope ++ opened)
    let penv ← getEnv
    match Parser.runParserCategory penv `command snippet with
    | .error _ => return some (snippet, none, penv)
    | .ok stx => return some (snippet, some stx, penv)

/-- A declaration's source and its comment ranges, `null` when it does not
parse alone; the caller then decides whether the source can be shown. -/
def declarationSource (c : Name) : CommandElabM (Option (String × Json)) := do
  let some (snippet, stx?, penv) ← declarationSnippet c | return none
  let comments := match stx? with
    | some stx => commentsJson snippet.toUTF8 stx snippet.utf8ByteSize
    | none => Json.null
  let mod := (moduleOf penv c).getD Name.anonymous
  if ← commentLikeToken penv mod snippet then return some (snippet, Json.null)
  return some (snippet, comments)

/-- Some declarations have a source range only through a parent that Lean's
environment structurally identifies, such as a constructor's inductive type.
Show that parent unless it is a theorem or axiom, whose source carries a proof.
An internal-looking name is not provenance: project metaprograms can create it. -/
partial def companionSource (c : Name) : CommandElabM (Option (String × Json)) := do
  let env ← getEnv
  let parent := c.getPrefix
  if (← findDeclarationRanges? c).isSome || !env.contains parent then return none
  if canonical env c == c then return none
  if (← findDeclarationRanges? parent).isNone then
    return ← companionSource parent
  -- A field's companions (`S.x._default`, `S.p._autoParam`) read best in the
  -- structure that declares the field.
  let shown := canonical env parent
  let kind := kindOf env shown
  if kind == "theorem" || kind == "axiom" then return none
  declarationSource shown

/-- The node where a parsed declaration's value starts, ending its statement. -/
def valueNode? (stx : Syntax) : Option Syntax :=
  let decl := (findKind? stx ``Parser.Command.declaration).getD stx
  (findKind? decl ``Parser.Command.declValSimple).orElse fun _ =>
    (findKind? decl ``Parser.Command.declValEqns).orElse fun _ =>
      findKind? decl ``Parser.Command.whereStructInst

/-- The statement of a declaration that does not parse whole, as when only its
proof uses `local notation`: cut at successive `:=` tokens and parse the prefix
with `:= sorry` as its value. The parsed value's start is the real statement
boundary. A cut inside a structure-style proof therefore recovers its preceding
`where`; one inside the statement (`let x := …`) does not parse as a command. -/
def statementPrefix? (penv : Environment) (snippet : String) :
    Option (String × Syntax) := Id.run do
  let mut written := ""
  for part in (snippet.splitOn ":=").dropLast do
    written := written ++ part
    if let .ok stx := Parser.runParserCategory penv `command (written ++ ":= sorry") then
      if let some v := valueNode? stx then
        if let some pos := v.getPos? then
          if Nat.ble pos.byteIdx written.utf8ByteSize then
            let statementBytes := snippet.toUTF8.extract 0 pos.byteIdx
            return some (String.fromUTF8! statementBytes, stx)
    written := written ++ ":="
  return none

/-- The declaration's source up to its value: the statement as written, without
the proof, with its comment ranges. Parsed with Lean's own parser rather than
cut by pattern matching. -/
def statementSource (root : Name) : CommandElabM (Option (String × Json)) := do
  let env ← getEnv
  let some (snippet, stx?, penv) ← declarationSnippet root | return none
  -- `parsed` is the text `stx` was parsed from; it starts with `written`.
  let shown (written parsed : String) (stx? : Option Syntax) :
      CommandElabM (Option (String × Json)) := do
    if ← commentLikeToken penv ((moduleOf env root).getD Name.anonymous) written then
      return some (written, Json.null)
    return some (written, match stx? with
      | some stx => commentsJson parsed.toUTF8 stx written.utf8ByteSize
      | none => Json.null)
  let kind := kindOf env root
  -- A type declaration has no value to strip: all of it is the statement.
  if kind == "structure" || kind == "class" || kind == "inductive" then
    return ← shown snippet.trimAsciiEnd.toString snippet stx?
  let some stx := stx? | do
    let some (written, stx) := statementPrefix? penv snippet | return none
    shown written.trimAsciiEnd.toString (written ++ ":= sorry") (some stx)
  let some v := valueNode? stx | do
    if kind == "axiom" || kind == "opaque" then
      return ← shown snippet.trimAsciiEnd.toString snippet (some stx)
    return none
  let some pos := v.getPos? | return none
  -- `pos` is a byte position: cut by bytes, not by characters, or every `∀`
  -- before the value pushes the cut past it.
  let bytes := snippet.toUTF8.extract 0 pos.byteIdx
  shown (String.fromUTF8! bytes).trimAsciiEnd.toString snippet (some stx)

def rangeJson (c : Name) : CommandElabM Json := do
  match ← findDeclarationRanges? c with
  | some r => return Json.arr #[r.range.pos.line, r.range.endPos.line]
  | none   => return Json.null

def emit (cache : IO.Ref SemanticCache) (request : String)
    (fields : List (String × Json)) : CommandElabM Unit :=
  emitRecord cache <| Json.mkObj (("root", Json.str request) :: fields)

/-- Emit one entry of a table shared by every root, the first time a root
needs it. Roots name their trusted declarations, external semantic material,
and boundary modules, so what many roots share is stated once per run. -/
def emitShared (cache : IO.Ref SemanticCache)
    (emitted : IO.Ref (Std.HashSet (String × Name))) (table : String) (name : Name)
    (value : CommandElabM Json) : CommandElabM Unit := do
  unless (← emitted.get).contains (table, name) do
    emitted.modify (·.insert (table, name))
    let entry := Json.mkObj [
      ("table", Json.str table), ("name", Json.str (toString name)), ("value", ← value)]
    emitRecord cache entry

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
    (expandCache : IO.Ref (Std.HashMap Name (Array Name)))
    (semanticCache : IO.Ref SemanticCache)
    (emitted : IO.Ref (Std.HashSet (String × Name)))
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
    expandedMeaning expandCache env projectRoots c
  let mut trusted : Array Name := #[]
  let mut edges : Array (Name × Array Name) := #[]
  let mut assumed : Array Name := #[]
  let mut work : Array Name := #[root]
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
  let axioms ← collectAxioms root
  -- Axioms are part of the trust boundary even when reached only through a
  -- theorem proof. Their types can name project definitions or external
  -- notions whose meaning must be bound just like statement dependencies.
  for axiomName in axioms do
    for d in ← expand axiomName do
      if isLocal d then
        if !trusted.contains d && d != root then
          trusted := trusted.push d
          work := work.push d
      else if !isCore d && !assumed.contains d then
        assumed := assumed.push d
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
    emitShared semanticCache emitted "module" mod do
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
  -- A trusted declaration's record does not depend on the root: its
  -- dependencies are its own meaning's local constants.
  for c in trusted.qsort Name.lt do
    emitShared semanticCache emitted "trusted" c do
      let deps := (edges.find? (·.1 == c)).map (·.2) |>.getD #[]
      let kind := kindOf env c
      let (source, sourceComments) ← if kind == "theorem" || kind == "axiom" then
        pure (Json.null, Json.null)
      else
        match ← declarationSource c with
        | some (s, comments) => pure (Json.str s, comments)
        | none => match ← companionSource c with
          | some (s, comments) => pure (Json.str s, comments)
          | none => pure (Json.null, Json.null)
      return Json.mkObj [
        ("name", Json.str (toString c)),
        ("source_name", Json.str (toString (privateToUserName c))),
        ("kind", Json.str kind),
        ("module", Json.str (toString ((moduleOf c).getD Name.anonymous))),
        ("range", ← rangeJson c),
        ("signature", Json.str (← signatureOf c)),
        ("raw_signature", Json.str (← rawSignatureOf c)),
        ("semantic_schema", Json.str semanticSchema),
        ("semantic", ← cachedSemanticMaterial semanticCache env c),
        ("depends", Json.arr (deps.map fun d => Json.str (toString d))),
        ("source", source),
        ("source_comments", sourceComments)]
    items := items.push (Json.str (toString c))
  let rootDeps := (edges.find? (·.1 == root)).map (·.2) |>.getD #[]
  let (statement, statementComments) := match ← statementSource root with
    | some (s, comments) => (Json.str s, comments)
    | none => (Json.null, Json.null)
  let rootKind := kindOf env root
  let (source, sourceComments) ← if rootKind == "theorem" || rootKind == "axiom" then
    pure (Json.null, Json.null)
  else
    match ← declarationSource root with
    | some (s, comments) => pure (Json.str s, comments)
    | none => pure (Json.null, Json.null)
  for d in sortedAssumed ++ sortedAxioms do
    emitShared semanticCache emitted "semantic" d do
      cachedSemanticMaterial semanticCache env d
  emit semanticCache request <| [
    ("found", Json.bool true),
    ("statement_source", statement),
    ("statement_comments", statementComments),
    ("source", source),
    ("source_comments", sourceComments),
    ("kind", Json.str rootKind),
    ("lean_version", Json.str Lean.versionString),
    ("module", Json.str (toString ((moduleOf root).getD Name.anonymous))),
    ("range", ← rangeJson root),
    ("signature", Json.str (← signatureOf root)),
    ("raw_signature", Json.str (← rawSignatureOf root)),
    ("semantic_schema", Json.str semanticSchema),
    ("semantic", ← cachedSemanticMaterial semanticCache env root),
    ("depends", Json.arr (rootDeps.map fun d => Json.str (toString d))),
    ("trusted", Json.arr items),
    ("assumed", Json.arr (sortedAssumed.map fun d => Json.str (toString d))),
    ("boundary_modules", Json.arr boundaryModuleNames),
    ("axioms", Json.arr (sortedAxioms.map fun d => Json.str (toString d)))]

/-- The probe's entry point: the skeleton of each requested root, keyed by the
name as the CLI spelled it. It runs in the probe's command scope, which has no
`open` and no option set, so signatures print with only `packetOptions` added. -/
def main (projectRoots : List Name) (roots : List (String × Name)) : CommandElabM Unit :=
  -- Elaborated proofs can be large; reading them has no heartbeat budget.
  withScope (fun scope => {{ scope with opts := maxHeartbeats.set scope.opts 0 }}) do
  let expandCache : IO.Ref (Std.HashMap Name (Array Name)) ← IO.mkRef {{}}
  let emitted : IO.Ref (Std.HashSet (String × Name)) ← IO.mkRef {{}}
  let coreModules ← AutoformSkeleton.toolchainModules (← getEnv)
  -- A command's `IO.println` output is captured and printed as one message when
  -- the command ends, at a cost quadratic in its size: on a Mathlib project that
  -- outlasts the probe itself. Write records to the file the CLI names instead,
  -- without redirecting incidental stdout into that trusted record stream.
  let direct ← (← IO.getEnv "{output_env}").mapM fun path => IO.FS.Handle.mk path .write
  let semanticCache : IO.Ref AutoformSkeleton.SemanticCache ← IO.mkRef {{ output := direct }}
  try
    for (request, root) in roots do
      AutoformSkeleton.skeleton projectRoots coreModules expandCache semanticCache emitted request root
  finally
    if let some out := direct then out.flush

end AutoformSkeleton

-- Each probe finds this module after the project's own search path, so a
-- project or dependency module of the same name would be imported instead.
run_cmd do
  let helper := Name.mkSimple "{helper_module}"
  if let some path ← (← searchPathRef.get).findWithExt "olean" helper then
    throwError "the project's search path already provides a module named {{helper}}, at {{path}}; \
      the skeleton probe needs that module name for its own helpers"
