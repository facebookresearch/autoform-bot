Rate how feasible it is for a strong autonomous Lean 4 agent, working alone for about {{minutes}} minutes
with the project's dependencies (such as Mathlib), to prove this statement exactly as it is stated:
5 = routine with the libraries, 4 = doable with some work, 3 = hard but possible, 2 = needs substantial
missing theory, 1 = research level or out of reach. Do not run anything; judge from the text below.

Title: {{title}}
Article: {{article_path}}

```lean
{{statement}}
```

Answer with one JSON object and nothing else: {"score": N, "reason": "one sentence"}.
