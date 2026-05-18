// F45 (PR #29 review): post-process the openapi-typescript output so that
// the ``ValidationError.ctx`` field type is usable from client code.
//
// FastAPI generates a schema where ``ctx`` has no defined properties.
// openapi-typescript turns that into ``Record<string, never>`` (literally
// "an object with no properties allowed"), which is unhelpful — clients
// cannot read e.g. ``error.ctx.limit_value`` even though Pydantic populates
// such fields at runtime. We rewrite that single occurrence to
// ``Record<string, unknown>`` so client code can introspect ctx safely.
//
// Other ``Record<string, never>`` occurrences (webhooks, $defs) are
// genuinely empty placeholders — we leave those alone.

import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const target = resolve(__dirname, "..", "src", "generated", "api.ts");

const src = readFileSync(target, "utf8");

// Anchor the rewrite to the comment that openapi-typescript emits directly
// above the ctx field. Without the anchor we'd risk hitting unrelated
// ``Record<string, never>`` occurrences (webhooks, $defs).
const pattern = /(\/\*\* Context \*\/\s*\n\s*ctx\?:\s*)Record<string, never>;/;
const replacement = "$1Record<string, unknown>;";

if (!pattern.test(src)) {
  // No ValidationError schema in this build, or the codegen output changed
  // shape. Silent no-op rather than failing the build.
  process.exit(0);
}

const out = src.replace(pattern, replacement);
writeFileSync(target, out, "utf8");
console.log("post-process: rewrote ValidationError.ctx → Record<string, unknown>");
