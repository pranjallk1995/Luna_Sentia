# Luna Sentia

![Docker required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)
![Docker Compose v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED?logo=docker&logoColor=white)
![Ollama included](https://img.shields.io/badge/Ollama-included-111111?logo=ollama&logoColor=white)
![PostgreSQL included](https://img.shields.io/badge/PostgreSQL-included-4169E1?logo=postgresql&logoColor=white)
![NVIDIA GPU optional](https://img.shields.io/badge/NVIDIA_GPU-optional-76B900?logo=nvidia&logoColor=white)
![Status: usable](https://img.shields.io/badge/Status-usable-2EA043)

## The shared models, database, and network for the Luna apps

Luna Sentia runs the services every Luna app depends on: one PostgreSQL server
holding each app's database, one Ollama server and its models, one Adminer for
browsing the databases, and the private Docker network the apps share. The apps
bundle none of these: they join `luna_sentia_network`, keep their data in
`luna-sentia-db`, and call `http://luna-sentia-ollama:11434`.

> **Sentia must be running and healthy before any app starts.** Every app
> stores its data in Sentia's PostgreSQL and joins Sentia's network; an app
> started without them cannot reach its database and keeps restarting until
> Sentia is up. `luna.sh start` handles the order for you; see
> [Is Sentia healthy?](#is-sentia-healthy) to check by hand.

| App | Uses Sentia for |
| --- | --- |
| [Luna Visia](https://github.com/pranjallk1995/Luna_Visia) | Its database; image captions and tags (`VISION_MODEL`) |
| [Luna Fiora](https://github.com/pranjallk1995/Luna_Fiora) | Its database; parsing ambiguous bank messages (`OLLAMA_MODEL`) |
| [Luna Lexia](https://github.com/pranjallk1995/Luna_Lexia) | Its database; classifying documents and extracting their details (`DOCUMENT_MODEL`) |
| [Luna Vita](https://github.com/pranjallk1995/Luna_Vita) | Its database and the shared network (reaches Luna Visia through it) |
| [Luna Lumia](https://github.com/pranjallk1995/Luna_Lumia) | Its database and the shared network |

> **GPU requirement for the default model:** `qwen2.5vl:7b` is larger than 6 GB
> and, with the default `gpus: all` setting, needs an NVIDIA GPU with at least
> 8 GB of VRAM. See [CPU fallback](#cpu-fallback) to run without one.

## Architecture

| Component | Technology | Purpose |
| --- | --- | --- |
| Database | PostgreSQL 17 (`luna-sentia-db`) | One server; each app has its own role and database; never published on a host port |
| Database setup | `postgres-init` (one-shot) | Creates each app's role and database and syncs role passwords, then exits |
| Model server | Ollama | Serves local models to every Luna app; never published on a host port |
| Model pull | Ollama CLI (one-shot) | Pulls every model in `SENTIA_MODELS` at startup, then exits |
| Database UI | Adminer | Browses every app's database, on localhost port 19080 |
| Storage | `luna_sentia_postgres_data`, `luna_sentia_ollama_data` volumes | Every app's data and the downloaded models, kept across restarts |
| Network | `luna_sentia_network` | Shared private network every Luna app joins as an external network |

```text
Luna Visia / Vita / Lumia / Fiora / Lexia
        -> luna_sentia_network
        -> luna-sentia-db:5432 (PostgreSQL: lunavisia, lunavita, lunalumia, lunafiora, lunalexia)
        -> luna-sentia-ollama:11434 (Ollama)
```

Every service on the network uses a unique, project-prefixed alias, such as
`luna-sentia-db`, `luna-sentia-ollama`, or `luna-visia-mcp`, so the apps never
collide.

## Start everything

`luna.sh` starts Sentia first and waits until PostgreSQL and Ollama are healthy
and Adminer is up, runs `postgres-init` so every app's database exists, then
builds and starts each app in turn, waiting until its services are running or
healthy. It
works from this repository (`./luna.sh`) or copied into the folder that holds
`Luna_Sentia` and the app folders (`../luna.sh` from here, as below):

```sh
../luna.sh start                    # Sentia + visia, vita, lumia, fiora, lexia
../luna.sh start vita lumia         # Sentia + only the named apps
../luna.sh start --no-build         # skip image rebuilds
../luna.sh start --watch            # then run Compose Watch for every app
../luna.sh status                   # containers of each stack
../luna.sh stop                     # stop every app, then Sentia
../luna.sh stop vita                # stop one app; Sentia keeps running
```

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
```

## Is Sentia healthy?

Start an app only once Sentia reports healthy:

```sh
docker compose ps        # postgres and ollama: "Up ... (healthy)"; adminer: "Up"
```

- `postgres (healthy)`: the server accepts connections (`pg_isready`).
- `ollama (healthy)`: the model server answers (`ollama list`).
- `postgres-init` printed `Ready: luna<app>` for every app with a password set.

If `postgres` is not healthy, check `docker compose logs postgres`; an app
started now will restart until the database answers. Models can still be
downloading; that only delays the first model call, not startup.

The first start pulls every model in `SENTIA_MODELS` (`qwen2.5vl:7b` by
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
SENTIA_MODELS=qwen2.5vl:7b llama3.2-vision:11b
```

Restart with `docker compose up -d` to pull the new models. Each app then
selects which served model it uses in its own `.env` (`VISION_MODEL` in Visia,
`OLLAMA_MODEL` in Fiora, `DOCUMENT_MODEL` in Lexia). A model an app asks for that is not listed here fails
when that app first calls it.

Ollama loads models on demand and unloads them after a few idle minutes, so
apps sharing one model share its memory, while different models compete for VRAM.

## CPU fallback

If NVIDIA passthrough is unavailable, remove `gpus: all` from the `ollama`
service. Every app keeps working, but model calls are slower.

## Shared PostgreSQL

One PostgreSQL 17 server, `luna-sentia-db`, holds every app's data. Each app
gets its own login role and database of the same name (`lunavisia`, `lunavita`,
`lunalumia`, `lunafiora`, `lunalexia`) and owns only that database, so apps
cannot read each other's data. Each app creates and upgrades its own tables
when it starts.

`postgres-init` (`postgres/init-databases.sh`) creates any missing role or
database and resets each role's password from Sentia's `.env`; it never drops
or changes data, so it is safe to run on every start. Set in `.env`:

| Variable | Purpose |
| --- | --- |
| `SENTIA_POSTGRES_PASSWORD` | The `sentia` superuser; used only by `postgres-init` and for maintenance |
| `LUNAVISIA_DB_PASSWORD`, `LUNAVITA_DB_PASSWORD`, `LUNALUMIA_DB_PASSWORD`, `LUNAFIORA_DB_PASSWORD`, `LUNALEXIA_DB_PASSWORD` | Each app's role password; **must equal `POSTGRES_PASSWORD` in that app's `.env`** |

An app whose password is unset is skipped. After changing a password in both
places, run `docker compose run --rm postgres-init` and restart that app.

Back up and restore one app's database:

```sh
docker compose exec -T postgres pg_dump -U sentia -Fc lunavita > lunavita.dump
docker compose exec -T postgres pg_restore -U sentia -d lunavita --clean --if-exists \
    --no-owner --role=lunavita < lunavita.dump
```

Open a SQL shell: `docker compose exec postgres psql -U sentia -d lunavita`.

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

Username `sentia` with `SENTIA_POSTGRES_PASSWORD` sees every database.

## Operations

- **Stopping:** stop the apps first, then Sentia (`../luna.sh stop` does both in
  order). `docker compose down` stops PostgreSQL and Ollama but keeps their
  volumes; the network is removed only when no Luna app is still attached.
- **Privacy:** PostgreSQL and Ollama have no host port; only containers on
  `luna_sentia_network` can reach them. Adminer listens only on localhost.
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
