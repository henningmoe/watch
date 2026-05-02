# FreshRSS on Railway

## Deploy

1. Create a new Railway project and connect this repository.
2. Set the root directory to `/` (Railway reads `freshrss/railway.toml` automatically).
3. Add a PostgreSQL plugin to the project.
4. Set the environment variables listed below.
5. Deploy — Railway builds `freshrss/Dockerfile` and exposes the service on the generated domain.

## Required environment variables

| Variable | Description |
|---|---|
| `PORT` | Set automatically by Railway |
| `FRESHRSS_ENV` | Set to `production` |
| `CRON_MIN` | Cron schedule for feed refresh, e.g. `*/15` |
| `DB_HOST` | PostgreSQL host (from Railway plugin: `${{Postgres.PGHOST}}`) |
| `DB_PORT` | PostgreSQL port (`${{Postgres.PGPORT}}`) |
| `DB_NAME` | Database name (`${{Postgres.PGDATABASE}}`) |
| `DB_USER` | Database user (`${{Postgres.PGUSER}}`) |
| `DB_PASSWORD` | Database password (`${{Postgres.PGPASSWORD}}`) |

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
complete setup, or use the `freshrss-cli` auto-install environment variables
(`FRESHRSS_INSTALL_ADMIN_*`) to skip the wizard entirely.
