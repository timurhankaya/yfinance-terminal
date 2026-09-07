// The two environment reads the Playwright run makes, declared rather
// than pulled in with `@types/node`.
//
// Adding "node" to `types` would put Node's globals in scope for `src`
// as well, and the terminal is a browser bundle: a `Buffer` or a
// `process.env` that type-checked there would fail at runtime with
// nothing to catch it. This declaration covers exactly what
// `playwright.config.ts` and the spec use.
declare const process: { env: Record<string, string | undefined> };
