# Quick start

The shortest path from zero to a running, safe install. Run these from a shell on the
host running your Paperless-NGX compose stack. For the full annotated walkthrough
(scoped token, secrets, from-source install), see [Installation](installation.md).

## 1. Add the service

```bash
# Print the compose service block:
docker run --rm ghcr.io/bradsp/paperless-assistant:latest init
```

Paste the printed `paperless-assistant:` block into your Paperless-NGX
`docker-compose.yml`, alongside your `webserver` service.

## 2. Set your secrets

Create a `.env` file next to your `docker-compose.yml` (compose reads it
automatically). **Secrets go here, never in a YAML config file.**

```dotenv
# .env
PAPERLESS_ASSISTANT_TOKEN=your-scoped-paperless-api-token
ANTHROPIC_API_KEY=sk-ant-...
```

Don't have an AI key yet? See [AI providers](ai-providers.md) for exactly where to get
one (or set up local Ollama for zero cloud egress).

## 3. Bring it up, provision, verify

```bash
docker compose up -d paperless-assistant

docker compose exec paperless-assistant pa setup     # create custom fields + review tags (idempotent)
docker compose exec paperless-assistant pa doctor    # verify connectivity, token, provider, config
```

## 4. First run is a safe dry-run

```bash
docker compose exec paperless-assistant pa run       # first run = dry-run: reports, writes nothing
cat ./pa-data/run-reports/sweep-*.json               # what it WOULD change
```

## 5. Turn on writes

Once you're happy with the proposals:

```bash
docker compose exec paperless-assistant pa run --write
```

From here the scheduled `serve` sweep keeps your library tidy on an interval (hourly by
default). Every run is idempotent, so re-running is always safe.

---

**Next step:** [Usage](usage.md) for day-to-day operation, or
[Installation](installation.md) for the complete walkthrough. See the
[documentation index](README.md) for everything.
