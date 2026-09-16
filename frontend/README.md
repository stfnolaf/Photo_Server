# Frontends

Each frontend lives in its own directory and consumes the Photo Server backend API. Frontends own their build, runtime configuration, assets, and tests so additional clients can be added without changing the backend package.

Current frontend:

- [`web`](web/README.md): the responsive React library and photo workspaces. Its domain, API, durable-mutation, and design-token boundaries are suitable for reuse by a later desktop or mobile shell.
