import { defineConfig } from "@hey-api/openapi-python";

// Drift-check twin of openapi-python.config.ts, used by `npm run check:api`.
// Identical to the main config except for the output path (a throwaway dir
// inside node_modules, gitignored), so regenerating with it must produce
// byte-identical files to the committed src/photo_server/generated.
//
// It exists because the CLI's `-o <path>` flag replaces the whole `output`
// config object (the generator's getOutput() then only keeps `{path}`,
// losing `pythonVersion` and falling back to its 3.9 defaults, which
// changes the emitted style — Optional/Union vs `| None`/StrEnum) and
// would make every check:api run fail. A second config file keeps the
// full output object intact; run it with an explicit -f and no -o.

export default defineConfig({
  input: "../openapi/openapi.json",
  output: {
    path: "node_modules/.openapi-python-check-tmp",
    pythonVersion: "3.12",
  },
});
