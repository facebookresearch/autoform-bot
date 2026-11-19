- **A claim whose lease has another schema names it.** A claim ref whose lease
  is JSON with a schema other than `autoform-claim/v1` was reported as having
  an invalid lease schema, so telling a ref another tool wrote from a damaged
  lease meant reading its commit. The message now says the schema is
  unsupported and names it: `claim 'K' has unsupported lease schema 'X'` (#811).
