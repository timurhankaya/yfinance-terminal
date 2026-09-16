// The environment reads the Playwright run makes, declared rather than
// pulled in with `@types/node`: adding "node" to `types` would put Node's
// globals in scope for `src`, and the terminal is a browser bundle where
// a `process.env` that type-checked would fail at runtime.
declare const process: { env: Record<string, string | undefined> };
