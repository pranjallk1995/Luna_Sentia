# Luna Sentia

![Docker required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)
![Docker Compose v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED?logo=docker&logoColor=white)
![Ollama included](https://img.shields.io/badge/Ollama-included-111111?logo=ollama&logoColor=white)
![NVIDIA GPU optional](https://img.shields.io/badge/NVIDIA_GPU-optional-76B900?logo=nvidia&logoColor=white)
![Status: usable](https://img.shields.io/badge/Status-usable-2EA043)

## The shared local model service, database UI, and network for the Luna apps

Luna Sentia runs one Ollama server and its models for every Luna app, one
Adminer for all of their databases, and owns the private Docker network those
apps share. Apps no longer bundle their own
Ollama: they join `luna_sentia_network` and call
`http://luna-sentia-ollama:11434`.

| App | Uses Sentia for |
| --- | --- |
| [Luna Visia](https://github.com/pranjallk1995/Luna_Visia) | Image captions and tags (`VISION_MODEL`) |
| [Luna Fiora](https://github.com/pranjallk1995/Luna_Fiora) | Parsing ambiguous bank messages (`OLLAMA_MODEL`) |
| [Luna Lexia](https://github.com/pranjallk1995/Luna_Lexia) | Classifying documents and extracting their details (`DOCUMENT_MODEL`) |
| [Luna Vita](https://github.com/pranjallk1995/Luna_Vita) | The shared network only (reaches Luna Visia through it) |
| [Luna Lumia](https://github.com/pranjallk1995/Luna_Lumia) | The shared network only |

> **GPU requirement for the default model:** `qwen2.5vl:7b` is larger than 6 GB
> and, with the default `gpus: all` setting, needs an NVIDIA GPU with at least
> 8 GB of VRAM. See [CPU fallback](#cpu-fallback) to run without one.

## Architecture

| Component | Technology | Purpose |
| --- | --- | --- |
| Model server | Ollama | Serves local models to every Luna app; never published on a host port |
| Model pull | Ollama CLI (one-shot) | Pulls every model in `SENTIA_MODELS` at startup, then exits |
| Database UI | Adminer | One UI for every Luna app's PostgreSQL, on localhost port 19080 |
| Storage | `luna_sentia_ollama_data` volume | Downloaded models, kept across restarts |
| Network | `luna_sentia_network` | Shared private network every Luna app joins as an external network |

```text
Luna Visia / Luna Fiora / Luna Lexia / future apps
        -> luna_sentia_network
        -> luna-sentia-ollama:11434 (Ollama)
        -> luna_sentia_ollama_data (models)
```

Every service on the network uses a unique, project-prefixed alias, such as
`luna-sentia-ollama`, `luna-visia-mcp`, or `luna-vita-db`, so the apps never
collide.

## Start everything

`luna.sh` starts Sentia first, waits for Ollama and Adminer, then builds and
starts each app in turn, waiting until its services are running or healthy. It
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

Start Sentia before any other Luna app, because their Compose files expect
`luna_sentia_network` to exist:

```sh
docker compose up -d
```

The first start pulls every model in `SENTIA_MODELS` (`qwen2.5vl:7b` by
default), which can take several minutes. Follow progress with:

```sh
docker compose logs -f ollama-pull
```

Then start the apps in any order, for example:

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

## Database UI

Adminer is available at <http://127.0.0.1:19080> and is bound to localhost. It
reaches each app's PostgreSQL over `luna_sentia_network`; no database port is
published to the host. Sign in with System `PostgreSQL`, the app's database
alias as Server, and that app's credentials:

| App | Server | Username and database | Password (default) |
| --- | --- | --- | --- |
| Luna Visia | `luna-visia-db` | `lunavisia` | `POSTGRES_PASSWORD` (`lunavisia-dev`) |
| Luna Vita | `luna-vita-db` | `lunavita` | `POSTGRES_PASSWORD` (`lunavita-dev`) |
| Luna Lumia | `luna-lumia-db` | `lunalumia` | `POSTGRES_PASSWORD` (`lunalumia-dev`) |
| Luna Fiora | `luna-fiora-db` | `lunafiora` | `POSTGRES_PASSWORD` (`lunafiora-dev`) |
| Luna Lexia | `luna-lexia-db` | `lunalexia` | `POSTGRES_PASSWORD` (`lunalexia-dev`) |

Use the alias, not `postgres`: every app has a service called `postgres`, so
that name is ambiguous on the shared network. A database is reachable only
while its app is running.

## Operations

- **Stopping:** `docker compose down` stops Ollama. The network is removed only
  when no Luna app is still attached; stop the apps first for a clean shutdown.
- **Privacy:** Ollama has no host port and no API key; only containers on
  `luna_sentia_network` can reach it. Adminer listens only on localhost.
- **Adding an app:** join the external network in the app's Compose file and
  give each service a unique alias:

```yaml
services:
  mcp:
    environment:
      OLLAMA_URL: http://luna-sentia-ollama:11434
    networks:
      sentia_network:
        aliases: [luna-myapp-mcp]

networks:
  sentia_network:
    external: true
    name: luna_sentia_network
```
