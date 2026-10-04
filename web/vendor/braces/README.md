# Temporary braces security backport

This is the MIT-licensed `braces@3.0.3` npm runtime, with its original name and
license retained. Replay maintains this local copy until a fixed upstream release
is available. It is not an official upstream release.

## Provenance and fix

- Base: https://registry.npmjs.org/braces/-/braces-3.0.3.tgz
- Base archive verified against the existing package-lock SHA-512 integrity.
- Advisory: https://github.com/advisories/GHSA-vfj7-8cjw-p6xm
- Reviewed upstream proposal: https://github.com/micromatch/braces/pull/72
- Proposal head: `28d440b5dd449dbf1fe6f3506cf94ecca4d02660`
- Only the proposal's diff for `lib/{constants,parse,compile,expand,stringify}.js`
  was applied to 3.0.3. Other changes on upstream master (quote parsing and comma
  handling) are intentionally excluded. `index.js`, `utils.js` and `LICENSE` are
  unchanged. Metadata contains only runtime dependencies and a private version.

Parsing rejects brace/parenthesis nesting beyond 100 levels, including unclosed
patterns. Recursive AST walkers enforce the same bound for supplied ASTs, and
expansion rejects cyclic parent chains. `maxDepth` can reduce but cannot disable
or raise the hard ceiling. Existing character and expansion limits remain.

## Installation and verification

The root `braces` file dependency and `$braces` override route transitive consumers
to this version. There is no postinstall patch or audit waiver. `npm ci` installs
the checked-in copy. Run `npm run test:client` and `npm audit --audit-level=low`
from `web/`. Security tests exercise the installed public entry points, direct
AST inputs, malformed patterns and the micromatch / fast-glob consumers.

`npm audit` checks registry dependencies; it does not certify this local fork.
Regression tests and review of the pinned source patch are required alongside
the unchanged all-severity registry audit. This backport addresses CVE-2026-93687,
not every possible resource-exhaustion input.

## Removal

After a fixed upstream release, replace the local dependency and override,
regenerate the lockfile, retain the security tests (adjust the resolution
assertion), and rerun client tests, builds and headless E2E before deleting this
directory. Do not remove the tests just to pass an upgrade.
