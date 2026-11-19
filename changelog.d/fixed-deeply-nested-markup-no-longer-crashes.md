- **Deeply nested markup no longer crashes `audit` and `render` on a coverage
  page.** Markup nested about a thousand elements deep anywhere on the page, a
  table caption included, raised a RecursionError when the coverage contract
  was read. The page's tables are now read with an explicit stack, as its text
  already was (#657).
