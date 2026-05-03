# FreshRSS on Railway

## Deploy

1. Create a new Railway project and connect this repository.
2. Set the **Root Directory** to `freshrss/` in the Railway service settings.
3. Add a PostgreSQL plugin to the project.
4. Set the environment variables listed below.
5. Deploy — Railway builds the Dockerfile and proxies traffic to port 80.

The official `freshrss/freshrss` image runs Nginx internally and needs no
configuration overrides. Railway's proxy handles TLS and public routing.

## Required environment variables

| Variable | Description |
|---|---|
| `FRESHRSS_ENV` | Set to `production` |
| `CRON_MIN` | Cron schedule for feed refresh, e.g. `*/15` |
| `DB_HOST` | PostgreSQL host (from Railway plugin: `${{Postgres.PGHOST}}`) |
| `DB_PORT` | PostgreSQL port (`${{Postgres.PGPORT}}`) |
| `DB_NAME` | Database name (`${{Postgres.PGDATABASE}}`) |
| `DB_USER` | Database user (`${{Postgres.PGUSER}}`) |
| `DB_PASSWORD` | Database password (`${{Postgres.PGPASSWORD}}`) |

`PORT` does not need to be set — the image always listens on 80 and Railway
proxies to it automatically.

## Connecting to PostgreSQL

In the Railway dashboard, add the Postgres plugin to the same project.
Reference the plugin's variables in your service using Railway's variable
reference syntax, e.g.:

```
DB_HOST=${{Postgres.PGHOST}}
DB_PORT=${{Postgres.PGPORT}}
DB_NAME=${{Postgres.PGDATABASE}}
DB_USER=${{Postgres.PGUSER}}
DB_PASSWORD=${{Postgres.PGPASSWORD}}
```

FreshRSS will use these to configure its PostgreSQL connection on first boot.
Run the web-based installer at `https://<your-domain>/install.php` to
complete setup, or use the `FRESHRSS_INSTALL_ADMIN_*` environment variables
to skip the wizard entirely.
