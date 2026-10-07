# Deploying on a VM (about 20 minutes)

1. A small Ubuntu VM (2 vCPU, 4 GB) with ports 80 and 443 open, and a DNS name pointing
   at it (a free `*.duckdns.org` or `*.nip.io` name works: `sewasetu.<IP>.nip.io`).
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Copy this repo to the VM, then:
   ```
   cp .env.example .env
   # fill PUBLIC_BASE_URL, DOMAIN, ADMIN_PASSWORD, SECRET_KEY, INTERNAL_API_KEY (openssl rand -hex 32)
   docker compose up -d --build
   docker compose logs -f scheduler   # first boot loads 100k rows (~1 min), then runs the deemed job
   ```
4. Check:
   - `https://<domain>/` portal, `https://<domain>/admin` officer login
   - `https://<domain>/.well-known/oauth-protected-resource/mcp/citizen` returns JSON
   - `https://<domain>/eval-dashboard`
5. Connect a real client:
   ```
   claude mcp add --transport http sewasetu-citizen https://<domain>/mcp/citizen
   claude mcp add --transport http sewasetu-officer https://<domain>/mcp/officer
   ```
   In Claude Code run `/mcp`, pick the server, sign in in the browser. Citizens: any
   10-digit mobile, OTP from `https://<domain>/__gateway/`. Officers: the admin login.
6. Run the evals (needs `ANTHROPIC_API_KEY` in `.env`):
   ```
   docker compose exec assistant python -m evals.smoke_tools          # tool paths, no model
   docker compose exec assistant python -m evals.run_evals            # personas + access checks
   ```
   Results appear on `/eval-dashboard`.

Before the first boot, if you want to see what the deemed-approval job will do to the
snapshot: `docker compose run --rm -e DRY_RUN=1 scheduler python /app/scripts/deemed_approval.py`.

If a client does not send the RFC 8707 `resource` parameter, sign-in succeeds but the MCP
server refuses the token (audience is the portal). Set `ACCEPT_PORTAL_AUDIENCE=1` on the
assistant service only if you must support such a client.
