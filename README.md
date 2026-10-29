# Luna Sentia

![Docker required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)
![Docker Compose v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED?logo=docker&logoColor=white)
![Ollama included](https://img.shields.io/badge/Ollama-included-111111?logo=ollama&logoColor=white)
![PostgreSQL included](https://img.shields.io/badge/PostgreSQL-included-4169E1?logo=postgresql&logoColor=white)
![NVIDIA GPU optional](https://img.shields.io/badge/NVIDIA_GPU-optional-76B900?logo=nvidia&logoColor=white)
![Status: usable](https://img.shields.io/badge/Status-usable-2EA043)

## The shared models, database, sign-in, and network for the Luna apps

Luna Sentia runs the services every Luna app depends on: one PostgreSQL server
holding each app's database, one Ollama server and its models, single sign-on
for every app, one Adminer for browsing the databases, and the private Docker
network the apps share. The apps bundle none of these: they join
`luna_sentia_network`, keep their data in `luna-sentia-db`, sign people in
through `luna-sentia-auth`, and call `http://luna-sentia-ollama:11434`.

> **Sentia must be running and healthy before any app starts.** Every app
> stores its data in Sentia's PostgreSQL, signs people in through Sentia's auth
> service, and joins Sentia's network; an app started without them cannot reach
> its database and keeps restarting until Sentia is up. `luna.sh start` handles the order for you; see
> [Is Sentia healthy?](#is-sentia-healthy) to check by hand.

| App | Uses Sentia for |
| --- | --- |
| [Luna Visia](https://github.com/pranjallk1995/Luna_Visia) | Its database; image captions and tags (`VISION_MODEL`) |
| [Luna Fiora](https://github.com/pranjallk1995/Luna_Fiora) | Its database; parsing ambiguous bank messages (`OLLAMA_MODEL`) |
| [Luna Lexia](https://github.com/pranjallk1995/Luna_Lexia) | Its database; classifying documents and extracting their details (`DOCUMENT_MODEL`); semantic search (`EMBEDDING_MODEL`, pgvector) |
| [Luna Vibia](https://github.com/pranjallk1995/Luna_Vibia) | Its database (pgvector); captions and tags for music (`CAPTION_MODEL`); reaches Luna Visia for cover art; plays tracks on the host's speakers through its own PulseAudio mount, not Sentia |
| [Luna Vita](https://github.com/pranjallk1995/Luna_Vita) | Its database and the shared network (reaches Luna Visia through it) |
| [Luna Lumia](https://github.com/pranjallk1995/Luna_Lumia) | Its database and the shared network |

> **GPU requirement for the default model:** `qwen3-vl:8b-instruct`, which Visia,
> Lexia, Fiora, and Vibia all use, is larger than 6 GB and, with the default `gpus: all` setting, needs an NVIDIA GPU with at least
> 8 GB of VRAM. See [CPU fallback](#cpu-fallback) to run without one.

## Architecture

### Luna ecosystem

![Luna ecosystem architecture](assets/luna-ecosystem.svg)

Every Luna app is a Streamlit UI plus a FastMCP service on Luna Sentia's
`luna_sentia_network`: its data lives in Sentia's PostgreSQL, people sign in
through Sentia's `auth`, and model calls go to Sentia's Ollama. Orange lines are
Visia image URLs that other apps use: food images for Vita, room images for
Lumia, and cover art for Vibia. Grey dashed lines are planned: links into Fiora
(tax details from Lexia, transaction image and icon URLs from Visia), and a
proposed unifying app that detects which Luna apps are installed, shows them in
one home, and opens each one with the shared sign-in.

| Component | Technology | Purpose |
| --- | --- | --- |
| Database | PostgreSQL 17 + pgvector (`luna-sentia-db`) | One server; each app has its own role and database, with the `vector` extension; never published on a host port |
| Database setup | `postgres-init` (one-shot) | Creates each app's role and database and syncs role passwords, then exits |
| Single sign-on | Starlette (`luna-sentia-auth`) | One Google sign-in for every app; host port 19090 |
| Model server | Ollama | Serves local models to every Luna app; never published on a host port |
| Model pull | Ollama CLI (one-shot) | Pulls every model in `SENTIA_MODELS` at startup, then exits |
| Database UI | Adminer | Browses every app's database, on localhost port 19080 |
| Storage | `luna_sentia_postgres_data`, `luna_sentia_ollama_data` volumes | Every app's data and the downloaded models, kept across restarts |
| Network | `luna_sentia_network` | Shared private network every Luna app joins as an external network |

```text
Luna Visia / Vita / Lumia / Fiora / Lexia / Vibia
        -> luna_sentia_network
        -> luna-sentia-db:5432 (PostgreSQL: lunavisia, lunavita, lunalumia, lunafiora, lunalexia, lunavibia)
        -> luna-sentia-ollama:11434 (Ollama)
        -> luna-sentia-auth:8000 (who is signed in)
```

Every service on the network uses a unique, project-prefixed alias, such as
`luna-sentia-db`, `luna-sentia-ollama`, or `luna-visia-mcp`, so the apps never
collide.

## Start everything

`luna.sh` starts Sentia first and waits until PostgreSQL and Ollama are healthy
and Adminer is up, runs `postgres-init` so every app's database exists, starts
the sign-on service and waits for it, then starts each app in turn, waiting until its services are running or
healthy. It
works from this repository (`./luna.sh`) or copied into the folder that holds
`Luna_Sentia` and the app folders (`../luna.sh` from here, as below):

```sh
../luna.sh start                    # Sentia + visia, vita, lumia, fiora, lexia, vibia
../luna.sh start vita lumia         # Sentia + only the named apps
../luna.sh start --build            # build images from the local source instead
../luna.sh start --no-pull          # use the images already on this machine
../luna.sh start --watch            # then run Compose Watch for every app
../luna.sh status                   # containers of each stack
../luna.sh stop                     # stop every app, then Sentia
../luna.sh stop vita                # stop one app; Sentia keeps running
```

### Published images

Every push to `main` that changes a service's folder builds its image in GitHub
Actions (`.github/workflows/images.yml` in each repository) and publishes it to
GitHub Container Registry as `ghcr.io/pranjallk1995/luna-<app>-<service>`,
tagged `latest` and with the commit SHA. The images are public and hold no
secrets: every setting and credential comes from each stack's `.env` at runtime.

A machine that only runs Luna therefore needs no source code: each stack's
`docker-compose.yaml` and `.env`, in folders named `Luna_Sentia` and
`Luna_<App>`, plus `luna.sh` beside them. Sentia's `postgres-init` script is part
of the postgres image. The source is needed only for `start --build` and
`--watch`.

`start` pulls the latest images (for Sentia, only its own `postgres` and `auth`;
Ollama and Adminer update when you pull them yourself) and recreates any
container whose image changed, so running it again is also how you update. A
pull that fails, offline or before an image is first published, is skipped and
Compose builds that image locally. Use `--build` while changing code, so the
containers run your local source.

With `--watch`, the script stays in the foreground and runs one
`docker compose watch` per app, prefixing each line with the app's name (for
example `[vita] Syncing service "ui"`). Press Ctrl+C to stop watching; the
containers keep running. Sentia itself has no source to watch.

`stop` runs `docker compose down`, which removes containers but keeps every
volume and its data. Set `LUNA_APPS` to change the default app list, for
example `LUNA_APPS="visia vita" ../luna.sh start`. The script expects each app
in a folder named `Luna_<App>` beside it and beside `Luna_Sentia`.

## Start Sentia only

Copy `.env.example` to `.env` and set the database passwords (see
[Shared PostgreSQL](#shared-postgresql)), then start Sentia and wait for it:

```sh
docker compose up -d --wait postgres ollama adminer
docker compose run --rm postgres-init
docker compose up -d --build --wait auth
```

## Is Sentia healthy?

Start an app only once Sentia reports healthy:

```sh
docker compose ps        # postgres, ollama, auth: "Up ... (healthy)"; adminer: "Up"
```

- `postgres (healthy)`: the server accepts connections (`pg_isready`).
- `ollama (healthy)`: the model server answers (`ollama list`).
- `auth (healthy)`: the sign-on service answers <http://127.0.0.1:19090/health>.
- `postgres-init` printed `Ready: luna<app>` for every app with a password set.

If `postgres` is not healthy, check `docker compose logs postgres`; an app
started now will restart until the database answers. Models can still be
downloading; that only delays the first model call, not startup.

The first start pulls every model in `SENTIA_MODELS` (`qwen3-vl:8b-instruct` and `qwen3-embedding:0.6b` by
default), which can take several minutes. Follow progress with:

```sh
docker compose logs -f ollama-pull
```

Once Sentia is healthy, start the apps in any order, for example:

```sh
cd ../Luna_Visia && docker compose up -d --build
```

Check what is loaded and whether the GPU is used:

```sh
docker compose exec ollama ollama list
docker compose exec ollama ollama ps
nvidia-smi
```

## Models

`SENTIA_MODELS` is a space-separated list of the models to pull. To change it,
copy `.env.example` to `.env` and edit it:

```dotenv
SENTIA_MODELS=qwen3-vl:8b-instruct qwen3-embedding:0.6b llama3.2-vision:11b
```

Restart with `docker compose up -d` to pull the new models. Each app then
selects which served model it uses in its own `.env` (`VISION_MODEL` in Visia,
`OLLAMA_MODEL` in Fiora, `DOCUMENT_MODEL` and `EMBEDDING_MODEL` in Lexia, `CAPTION_MODEL` in Vibia).
The default list includes `qwen3-embedding:0.6b` (about 640 MB), the embedding
model behind Lexia's semantic search. A model an app asks for that is not listed here fails
when that app first calls it.

Ollama loads models on demand and unloads them after a few idle minutes, so
apps sharing one model share its memory, while different models compete for VRAM.
A model is also reloaded whenever a request asks for a different context size,
so every app asks for the same 16,384 tokens and one copy of Qwen3-VL serves
them all.

Three settings in `.env` tune the server for a 12 GB GPU:

| Variable | Value | Why |
| --- | --- | --- |
| `OLLAMA_FLASH_ATTENTION` | `1` | Needed for a quantized KV cache; less memory per token |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Halves the KV cache: Qwen3-VL at 16k takes 6.6 GB instead of 7.8 GB |
| `OLLAMA_NUM_PARALLEL` | `1` | Ollama does not run Qwen3-VL requests in parallel ("model architecture does not currently support parallel requests"), so requests from the apps queue |

## CPU fallback

If NVIDIA passthrough is unavailable, remove `gpus: all` from the `ollama`
service. Every app keeps working, but model calls are slower.

## Shared PostgreSQL

One PostgreSQL 17 server, `luna-sentia-db`, holds every app's data. Each app
gets its own login role and database of the same name (`lunavisia`, `lunavita`,
`lunalumia`, `lunafiora`, `lunalexia`, `lunavibia`) and owns only that database, so apps
cannot read each other's data. Each app creates and upgrades its own tables
when it starts.

The server is the official `postgres:17-alpine` image with
[pgvector](https://github.com/pgvector/pgvector) 0.8.1 compiled in
(`postgres/Dockerfile`), so the existing databases keep their collation. pgvector
is not a trusted extension, so `postgres-init` installs `vector` in every app
database as the superuser.

`postgres-init` (`postgres/init-databases.sh`) creates any missing role or
database and resets each role's password from Sentia's `.env`; it never drops
or changes data, so it is safe to run on every start. Set in `.env`:

| Variable | Purpose |
| --- | --- |
| `SENTIA_POSTGRES_PASSWORD` | The `sentia` superuser; used only by `postgres-init` and for maintenance |
| `LUNAVISIA_DB_PASSWORD`, `LUNAVITA_DB_PASSWORD`, `LUNALUMIA_DB_PASSWORD`, `LUNAFIORA_DB_PASSWORD`, `LUNALEXIA_DB_PASSWORD`, `LUNAVIBIA_DB_PASSWORD` | Each app's role password; **must equal `POSTGRES_PASSWORD` in that app's `.env`** |

An app whose password is unset is skipped. After changing a password in both
places, run `docker compose run --rm postgres-init` and restart that app.

Back up and restore one app's database:

```sh
docker compose exec -T postgres pg_dump -U sentia -Fc lunavita > lunavita.dump
docker compose exec -T postgres pg_restore -U sentia -d lunavita --clean --if-exists \
    --no-owner --role=lunavita < lunavita.dump
```

Open a SQL shell: `docker compose exec postgres psql -U sentia -d lunavita`.

## Single sign-on

`auth` gives every Luna app one shared sign-in. The first app you open sends you
to Sentia, which signs you in with Google once and sets an HTTP-only, secure
`luna_sso` cookie for the parent domain of all the apps (`lunaapps.fyi`). Every
app then reads that cookie, asks Sentia over `luna_sentia_network` whose session
it is (`GET /session`), and applies its own `LUNA<APP>_ALLOWED_EMAILS`. Opening
another app needs no second sign-in, and **Sign out** in any app ends the session
for all of them (other open apps notice within a minute). Sessions last
`SENTIA_AUTH_SESSION_DAYS` (30) and are stored, hashed, in the `lunaauth`
database.

| Route | Purpose |
| --- | --- |
| `/login?next=<app url>` | Sign in with Google (skipped when already signed in), then return to the app |
| `/callback` | Google's redirect back to Sentia |
| `/logout?next=<app url>` | End the session for every app, then return |
| `/session` | For app servers: the account behind the `X-Luna-Session` token |
| `/` | Who is signed in, with a sign-out link |

Only addresses under `SENTIA_AUTH_COOKIE_DOMAIN` (or localhost, when it is
blank) are accepted as `next`, so the service cannot be used to redirect
anywhere else.

Set up once:

1. **Public address.** Route a public hostname to `http://localhost:19090`, for
   example `auth.lunaapps.fyi` as a Public Hostname of your Cloudflare tunnel,
   and set it as `SENTIA_AUTH_PUBLIC_URL`. It must share the apps' parent
   domain, set as `SENTIA_AUTH_COOKIE_DOMAIN`.
2. **Google client.** Use a Google OAuth "Web application" client (one of the
   apps' UI clients works) and add `<SENTIA_AUTH_PUBLIC_URL>/callback` to its
   authorized redirect URIs. Set `SENTIA_AUTH_GOOGLE_CLIENT_ID` and
   `SENTIA_AUTH_GOOGLE_CLIENT_SECRET`.
3. **Secrets and accounts.** Set `SENTIA_AUTH_SECRET` (`openssl rand -hex 32`),
   `LUNAAUTH_DB_PASSWORD`, and `SENTIA_AUTH_ALLOWED_EMAILS`, the accounts that
   may sign in at all; each app narrows this with its own allowlist.
4. **Apps.** In each app's `.env`, set `LUNA_SSO_URL` to the public address and
   `LUNA_SSO_INTERNAL_URL` to `http://luna-sentia-auth:8000`.

For use only on localhost, leave `SENTIA_AUTH_COOKIE_DOMAIN` blank and set
`SENTIA_AUTH_PUBLIC_URL` and every `LUNA_SSO_URL` to `http://localhost:19090`;
browsers share localhost cookies across ports, so the apps still share one
sign-in. Tests: `docker compose run --rm --no-deps -v "$PWD/auth/tests:/workspace/auth/tests" auth python -m unittest discover -s tests -t .`

## Database UI

Adminer is available at <http://127.0.0.1:19080> and is bound to localhost. It
reaches `luna-sentia-db` over `luna_sentia_network`; no database port is
published to the host. Sign in with System `PostgreSQL`, Server
`luna-sentia-db`, and the app's credentials:

| App | Username and database | Password (default) |
| --- | --- | --- |
| Luna Visia | `lunavisia` | `POSTGRES_PASSWORD` (`lunavisia-dev`) |
| Luna Vita | `lunavita` | `POSTGRES_PASSWORD` (`lunavita-dev`) |
| Luna Lumia | `lunalumia` | `POSTGRES_PASSWORD` (`lunalumia-dev`) |
| Luna Fiora | `lunafiora` | `POSTGRES_PASSWORD` (`lunafiora-dev`) |
| Luna Lexia | `lunalexia` | `POSTGRES_PASSWORD` (`lunalexia-dev`) |
| Luna Vibia | `lunavibia` | `POSTGRES_PASSWORD` (`lunavibia-dev`) |

Username `sentia` with `SENTIA_POSTGRES_PASSWORD` sees every database.

## Operations

- **Stopping:** stop the apps first, then Sentia (`../luna.sh stop` does both in
  order). `docker compose down` stops PostgreSQL and Ollama but keeps their
  volumes; the network is removed only when no Luna app is still attached.
- **Privacy:** PostgreSQL and Ollama have no host port; only containers on
  `luna_sentia_network` can reach them. Adminer and the sign-on service listen
  only on localhost; publish the sign-on service through your tunnel.
- **Adding an app:** add it to the loop in `postgres/init-databases.sh` and its
  `LUNA<APP>_DB_PASSWORD` to `.env`, `.env.example`, and the `postgres-init`
  service, run `postgres-init`, then join the external network in the app's
  Compose file with a unique alias per service:

```yaml
services:
  mcp:
    environment:
      DATABASE_URL: postgresql://lunamyapp:${POSTGRES_PASSWORD}@luna-sentia-db:5432/lunamyapp
      OLLAMA_URL: http://luna-sentia-ollama:11434
    networks:
      sentia_network:
        aliases: [luna-myapp-mcp]

networks:
  sentia_network:
    external: true
    name: luna_sentia_network
```
