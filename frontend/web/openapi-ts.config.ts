import { defineConfig } from "@hey-api/openapi-ts";

// Generated client for the Photo Server API (docs/openapi-codegen-plan.md
// Phase 5a). The input is the checked-in spec, which scripts/check_openapi.py
// keeps in sync with the backend; src/api/generated is committed, and
// `npm run check:api` fails if it drifts from this spec.
//
// Deliberate choices (see the plan):
// - no `baseUrl`: the spec defines no servers, so generated URLs stay
//   relative and keep flowing through the Vite dev proxy / nginx `/api`
//   proxy (the client facade applies API_ROOT as the SDK client's
//   `baseUrl` instead, which leaves this generated output prefix-agnostic);
// - `throwOnError` stays at its default `false`: errors come back in the
//   result, and ApiError normalization lives in the hand-written facade;
// - the SDK is left in its default flat strategy: one function per
//   operation, named from the Phase 4b operationIds;
// - the fetch client bundles its zero-dependency fetch runtime into the
//   generated output (`bundle: true`, the default), so the app adds no
//   npm runtime dependency.
export default defineConfig({
  input: "../../openapi/openapi.json",
  output: { path: "src/api/generated" },
  plugins: ["@hey-api/client-fetch", "@hey-api/typescript", "@hey-api/sdk"],
});
