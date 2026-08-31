# MAGI RC643 r75 public release

This branch is a privacy-isolated root snapshot of the public MAGI contracts,
agent framework, release/validation framework, tests, examples, and
de-identified architecture documentation corresponding to private
host-service correction commit
`bd4cd0ce360d6b5c6daef86f8cedfea2fca9bb26`. The active installed package
remains bound to source commit
`a20e603f9c4160d00f6c5703a9c4b898a8433212`; host-owned launcher state is a
separate deployment-layer contract and does not rewrite the sealed package.

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
