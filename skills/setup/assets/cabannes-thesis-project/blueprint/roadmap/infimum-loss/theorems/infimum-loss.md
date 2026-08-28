---
declaration: theorem
origin: cited
---

# Infimum loss

Let $f^*$ be a solution obtained from the disambiguated distribution. Then
$f^*$ minimizes the weak risk

$$
\mathcal R_S(f) = \mathbb E_{(X,S)\sim\tau}[L(f(X),S)],
$$

where the infimum loss is $L(z,S)=\inf_{y\in S}\ell(z,y)$.

## Sources

- [Thesis source map: `il:thm:infimum-loss`](../../../sources/thesis.md)

## Depends on

- [Eligibility](../definitions/eligibility.md)
