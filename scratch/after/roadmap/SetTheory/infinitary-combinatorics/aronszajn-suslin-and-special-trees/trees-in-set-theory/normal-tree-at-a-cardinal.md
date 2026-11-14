# Normal tree at a cardinal

Let κ be a cardinal and (α,≤) a well-founded partial order; for x∈α let rank(x) denote the rank
of x in the well-founded relation <. α is a *normal tree at κ* if:

1. $\alpha$ is a tree at $\kappa$, i.e.
    - $\forall x\in\alpha$, $\{y \mid y\lt x\}$ is a chain,
    - $\forall x\in\alpha,\ \exists a\lt \kappa.\mathrm{ord},\ x\in\mathrm{level}_\alpha(a)$,
    - $\forall a\lt \kappa.\mathrm{ord},\ \mathrm{level}_\alpha(a)\neq\emptyset$
      and $|\mathrm{level}_\alpha(a)|\lt \kappa$;
2. $\alpha$ has a root: $\exists r\in\alpha\ \forall x\in\alpha,\ r\le x$;
3. every node can be extended to any higher level below $\kappa$:

    $$
    \forall x\in\alpha\ \forall b\lt \kappa.\mathrm{ord}\;
    \bigl(\operatorname{rank}(x)\lt b \to
    \exists y\in\alpha\ (x\lt y \land \operatorname{rank}(y)=b)\bigr);
    $$

4. every node splits:

    $$
    \forall x\in\alpha\ \exists y,z\in\alpha\;
    \bigl(x\lt y \land x\lt z \land y\not\le z \land z\not\le y\bigr).
    $$
