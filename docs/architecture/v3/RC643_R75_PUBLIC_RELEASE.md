# MAGI RC643 r75 public release

This branch is a privacy-isolated root snapshot of the public MAGI contracts,
agent framework, release/validation framework, tests, examples, and
de-identified architecture documentation corresponding to private source
commit `ab398a44c99b9e83e6ba5c989312df29cf914006`.

It intentionally excludes production runtime state, case records, credentials,
browser profiles, real filesystem paths, private legal-service connectors, and
the retired WHALE/MELCHIOR federation implementation. A2A remains an inert,
proposal-only boundary; it has no production writer authority.

The snapshot must pass:

```console
python3 scripts/public_release_audit.py --public-isolation --strict
```

The public snapshot is documentation and source evidence. It is not the
canonical production bundle and cannot be used as proof of live availability.

The de-identified maintenance record for this promotion is
[RC643_R75_MAINTENANCE.md](RC643_R75_MAINTENANCE.md).
