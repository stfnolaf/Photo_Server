import { defineConfig } from "@hey-api/openapi-python";

// Generated Python client for the Photo Server API (docs/openapi-codegen-plan.md
// Phase 6). The input is the checked-in spec, which scripts/check_openapi.py
// keeps in sync with the backend; src/photo_server/generated is committed and
// `npm run check:api` fails if it drifts from this spec.
//
// Deliberate choices (see the plan):
// - no baseUrl: the spec defines no servers, so generated URLs stay relative
//   (prefix-agnostic), mirroring the web client's openapi-ts.config.ts;
// - the generated Sdk's method stubs take no parameters in
//   @hey-api/openapi-python 0.0.24, so photo_server.upload_client keeps its
//   hand-written httpx transport and validates every JSON exchange through
//   the generated models (generated/pydantic_gen) — the part of the output
//   that is faithful to the spec;
// - no output.postProcess: the raw generator output is the committed
//   artifact (deterministic across machines and versions of unrelated
//   tools); the one ruff I001 it trips is ignored for the generated
//   directory in pyproject.toml;
// - the generator is pinned exact in package.json: it is 0.x (early
//   development), same discipline as the web client's pinned openapi-ts;
// - never run `openapi-python --dry-run` with this config (or any): the
//   output directory is cleaned before the writes are skipped, so a bare
//   dry run deletes the committed src/photo_server/generated tree;
// - the drift check (`npm run check:api`) regenerates into a throwaway dir
//   using openapi-python.check.ts (same content, different output path) —
//   the CLI's -o flag would drop pythonVersion and change the emitted
//   style, so the check passes the config file explicitly instead.
export default defineConfig({
  input: "../openapi/openapi.json",
  output: {
    path: "src/photo_server/generated",
    pythonVersion: "3.12",
  },
});
