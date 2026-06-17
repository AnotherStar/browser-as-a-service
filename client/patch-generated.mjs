/**
 * Post-process generated.ts.
 *
 * openapi-zod-client renders an OpenAPI `const` discriminator as
 * `z.string().default("x")`, which breaks `z.discriminatedUnion("action", …)`
 * (the discriminator must be a literal/enum). We rewrite those `action`
 * fields to `z.literal("x").default("x")`.
 */
import { readFileSync, writeFileSync } from "node:fs";

const file = new URL("./src/generated.ts", import.meta.url);
let src = readFileSync(file, "utf8");

const before = src;
src = src.replace(
  /action: z\.string\(\)(?:\.optional\(\))?\.default\("([^"]+)"\)/g,
  (_m, value) => `action: z.literal("${value}").default("${value}")`,
);

if (src === before) {
  console.warn("patch-generated: no action discriminator fields matched");
} else {
  writeFileSync(file, src);
  const count = (before.match(/action: z\.string\(\)/g) || []).length;
  console.log(`patch-generated: rewrote ${count} action discriminator(s) to z.literal`);
}
