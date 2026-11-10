# Luna Sentia

![Docker required](https://img.shields.io/badge/Docker-required-2496ED?logo=docker&logoColor=white)
![Docker Compose v2](https://img.shields.io/badge/Docker_Compose-v2-2496ED?logo=docker&logoColor=white)
![Ollama included](https://img.shields.io/badge/Ollama-included-111111?logo=ollama&logoColor=white)
![PostgreSQL included](https://img.shields.io/badge/PostgreSQL-included-4169E1?logo=postgresql&logoColor=white)
![NVIDIA GPU optional](https://img.shields.io/badge/NVIDIA_GPU-optional-76B900?logo=nvidia&logoColor=white)
![Status: v1.3](https://img.shields.io/badge/Status-v1.3-2EA043)

<p align="center">
  <a href="assets/luna-sentia-showcase.mp4"><img src="assets/luna-sentia-showcase.webp" alt="Signing in, adding a person, hiding an app from everyone but the owner, then the model monitor timing a live Lexia question on the GPU and its history by app" width="900"></a>
</p>

<p align="center"><em>Signing in, adding a person, hiding an app from everyone but the owner, then the model monitor timing a live Lexia question on the GPU and its history by app. Select it for the sharper video.</em></p>

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
| [Luna Visia](https://github.com/pranjallk1995/Luna_Visia) | Its database (pgvector); image captions and tags (`VISION_MODEL`); embeds images with its own SigLIP 2 model on the GPU, not through Sentia's Ollama |
| [Luna Fiora](https://github.com/pranjallk1995/Luna_Fiora) | Its database; parsing ambiguous bank messages (`OLLAMA_MODEL`) |
| [Luna Lexia](https://github.com/pranjallk1995/Luna_Lexia) | Its database; classifying documents and extracting their details (`DOCUMENT_MODEL`); semantic search (`EMBEDDING_MODEL`, pgvector) |
| [Luna Vibia](https://github.com/pranjallk1995/Luna_Vibia) | Its database (pgvector); captions and tags for music (`CAPTION_MODEL`); listens to and embeds tracks with its own CLAP model on the GPU, not through Sentia's Ollama; reaches Luna Visia for cover art; plays tracks on the host's speakers through its own PulseAudio mount, not Sentia |
| [Luna Unia](https://github.com/pranjallk1995/Luna_Unia) | Single sign-on and the shared network, over which it checks every app's and Sentia's health; no database |
| [Luna Vita](https://github.com/pranjallk1995/Luna_Vita) | Its database and the shared network (reaches Luna Visia through it) |
| [Luna Lumia](https://github.com/pranjallk1995/Luna_Lumia) | Its database and the shared network (reaches Luna Vibia through it, so the lights follow the mood of the music Vibia plays) |

> **GPU requirement for the default model:** `qwen3-vl:8b-instruct`, which Visia,
> Lexia, Fiora, and Vibia all use, is larger than 6 GB and, with the default `gpus: all` setting, needs an NVIDIA GPU with at least
> 8 GB of VRAM. See [CPU fallback](#cpu-fallback) to run without one.

## Architecture

### Luna ecosystem

![Luna ecosystem architecture](assets/luna-ecosystem.svg)

Every Luna app but Luna Socia is a React UI plus a FastMCP service on Luna Sentia's
`luna_sentia_network`; Socia, the owner's chat with Luna, is one service with no MCP
server. Each app's data lives in Sentia's PostgreSQL, people sign in through Sentia's
`auth`, and model calls go through Sentia's model monitor, which times each one, samples
the GPU and keeps Socia's chat model resident, to its Ollama. Socia's pictures come from
Sentia's ComfyUI, which takes turns with Ollama on the GPU. Visia's SigLIP 2, Vibia's CLAP
and Lexia's Docling run inside those apps instead, each in a process that exits when idle.
Orange lines are Visia image URLs that other apps use: food images for Vita, room images
for Lumia, and cover art for Vibia. Grey dashed lines are planned: links into Fiora (tax
details from Lexia, transaction image and icon URLs from Visia). Luna Unia is the one home
for the other apps, on port 42069: it checks which Luna stacks are running and opens each
one with the shared sign-in. The purple line is the mood and beats of the track Vibia
plays, which Lumia's lights follow.

| Component | Technology | Purpose |
| --- | --- | --- |
| Database | PostgreSQL 17 + pgvector (`luna-sentia-db`) | One server; each app has its own role and database, with the `vector` extension; never published on a host port |
| Database setup | `postgres-init` (one-shot) | Creates each app's role and database and syncs role passwords, then exits |
| Accounts and sign-on | Starlette (`luna-sentia-auth`) | One Google sign-in for every app, the list of people, and sign-in for MCP connectors; host port 19090 |
| Model server | Ollama (`luna-sentia-ollama-engine`) | Serves local models to every Luna app; never published on a host port |
| Model monitor | Starlette (`luna-sentia-ollama`, `luna-sentia-monitor`) | Passes every app's Ollama call through, timing it, samples the GPU, and stores both; the owner's page is on host port 19081, and ComfyUI's page, behind the owner's sign-in, on 19082 |
| Model pull | Ollama CLI (one-shot) | Pulls every model in `SENTIA_MODELS` at startup, then exits |
| Image model | ComfyUI + Qwen-Image 2.1 (`luna-sentia-comfyui`) | Makes pictures for the apps (Luna Socia); takes turns with Ollama on the GPU; its page is on 19082, through the monitor's owner-only gate |
| Image model pull | curl (one-shot) | Downloads Qwen-Image 2.1's files (about 24 GB) once, then exits |
| Database UI | Adminer | Browses every app's database, on localhost port 19080 |
| Storage | `luna_sentia_postgres_data`, `luna_sentia_ollama_data`, `luna_sentia_comfyui_models` volumes | Every app's data and the downloaded models, kept across restarts |
| Network | `luna_sentia_network` | Shared private network every Luna app joins as an external network |

```text
Luna Visia / Vita / Lumia / Fiora / Lexia / Vibia
        -> luna_sentia_network
        -> luna-sentia-db:5432 (PostgreSQL: lunavisia, lunavita, lunalumia, lunafiora, lunalexia, lunavibia)
        -> luna-sentia-ollama:11434 (model monitor) -> luna-sentia-ollama-engine:11434 (Ollama)
        -> luna-sentia-auth:8000 (who is signed in)
```

Every service on the network uses a unique, project-prefixed alias, such as
`luna-sentia-db`, `luna-sentia-ollama`, or `luna-visia-mcp`, so the apps never
collide.

## Start everything

`luna.sh` starts Sentia first and waits until PostgreSQL and Ollama are healthy
and Adminer is up, runs `postgres-init` so every app's database exists, starts
the sign-on service and the model monitor and waits for them, then starts each app in turn, waiting until its services are running or
healthy. It
works from this repository (`./luna.sh`) or copied into the folder that holds
`Luna_Sentia` and the app folders (`../luna.sh` from here, as below). An app
whose folder is missing is skipped, so cloning only some apps is enough; an app
that fails to start does not hold up the others, and the script names it at the end
and exits with an error.

To run on one machine without a tunnel, clone `Luna_Sentia` and the apps you want
into one folder, copy each repository's `.env.example` to `.env` (its defaults are
for localhost), set the Google client and secrets in Sentia's `.env` (see
[Single sign-on](#single-sign-on)), and run `./Luna_Sentia/luna.sh start`. Each app
then opens at `http://localhost:<port>`: Visia 18501, Vita 18502, Lumia 18503,
Fiora 18504, Lexia 18505, Vibia 18506, Socia 18507, and Unia 42069.

```sh
./luna.sh start                    # Sentia + visia, vita, lumia, fiora, lexia, vibia, socia, unia
./luna.sh start vita lumia         # Sentia + only the named apps
./luna.sh start --build            # build images from the local source instead
./luna.sh start --no-pull          # use the images already on this machine
./luna.sh start --watch            # then run Compose Watch for every app
./luna.sh status                   # containers of each stack
./luna.sh stop                     # stop every app, then Sentia
./luna.sh stop vita                # stop one app; Sentia keeps running
```

### Published images

Every push to `main` that changes a service's folder builds its image in GitHub
Actions (`.github/workflows/images.yml` in each repository) and publishes it to
GitHub Container Registry as `ghcr.io/pranjallk1995/luna-<app>-<service>`,
tagged `latest` and with the commit SHA. Pushing a release tag such as `v1.2.3`
publishes every service under that version too. Each `docker-compose.yaml` names
the version it runs, so a release also updates the tags there. The images are
public and hold no secrets: every setting and credential comes from each stack's
`.env` at runtime.

A machine that only runs Luna therefore needs no source code: each stack's
`docker-compose.yaml` and `.env`, in folders named `Luna_Sentia` and
`Luna_<App>`, plus `luna.sh` beside them. Sentia's `postgres-init` script is part
of the postgres image. The source is needed only for `start --build` and
`--watch`.

`start` pulls the images each `docker-compose.yaml` names (for Sentia, only its
own `postgres`, `auth`, and `monitor`; Ollama and Adminer update when you pull
them yourself) and recreates any container whose image changed, so updating means
taking the newer `docker-compose.yaml` and running it again. A
pull that fails, offline or before an image is first published, is skipped and
Compose builds that image locally. Use `--build` while changing code, so the
containers run your local source. Those builds are tagged `:dev`, so local code never
takes a release's name; `LUNA_IMAGE_TAG=dev ./luna.sh start --no-pull` starts them again
without rebuilding.

Each image is built and its tests run in GitHub Actions before anything is published, by
the steps in `.github/workflows/service-images.yml`, which every repository's `images.yml`
calls. The release the footers and Luna Unia's cards show comes from the git tag, so
publishing a release takes these steps: set the new tag in the `docker-compose.yaml`, commit,
tag, and push the commit and tag together (`git push --atomic origin main v1.2.3`).

`common/` holds the files every app shares. Change them there, run `common/sync.sh` to copy
them into each app, and commit each app. The image workflow warns when a copy differs.

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
docker compose ps        # postgres, ollama, auth, monitor: "Up ... (healthy)"; adminer: "Up"
```

- `postgres (healthy)`: the server accepts connections (`pg_isready`).
- `ollama (healthy)`: the model server answers (`ollama list`).
- `auth (healthy)`: the sign-on service answers <http://127.0.0.1:19090/health>.
- `monitor (healthy)`: the model monitor answers <http://127.0.0.1:19081/health>. The
  apps reach Ollama through it, so while it is down their model calls fail.
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

| Model | Served by | Used by | Before changing it |
| --- | --- | --- | --- |
| `qwen3-vl:8b-instruct` | Ollama | Lexia (`DOCUMENT_MODEL`), Visia (`VISION_MODEL`), Vibia (`CAPTION_MODEL`), Fiora (`OLLAMA_MODEL`), Socia's photo descriptions (`LUNASOCIA_VISION_MODEL`) | `model_checks/check_model.py`, then Lexia's, Visia's and Vibia's `model_evaluations/` |
| `qwen3:14b` | Ollama, resident at `SENTIA_RESIDENT_CONTEXT` | Socia's chat (`LUNASOCIA_CHAT_MODEL`) | `model_checks/check_model.py --context 8192`, then Socia's `model_evaluations/` |
| `qwen3-embedding:0.6b` | Ollama | Lexia's semantic search (`EMBEDDING_MODEL`) | Re-embed with Lexia's `reindex_semantic_search` |
| Qwen-Image 2.1 Turbo | ComfyUI | Socia's pictures of Luna | A few pictures by hand |
| SigLIP 2 (`google/siglip2-base-patch16-224`) | Visia itself, GPU only | Visia's meaning search | Pinned in Visia's image; `backfill_metadata` re-embeds |
| CLAP (`laion/clap-htsat-unfused`) | Vibia itself, GPU or CPU | Vibia's listening and search | Pinned in Vibia's image; every track is heard again |
| Docling (layout, tables, OCR) | Lexia itself, GPU or CPU | Lexia's page reading | Pinned in Lexia's image |
| Whisper `large-v3-turbo`, Kokoro 82M | Socia itself, CPU | Socia's voice messages and Luna's voice (`LUNASOCIA_STT_MODEL`, `LUNASOCIA_TTS_VOICE`) | Listen to a few replies; downloaded on first use |

See [Changing a model](#changing-a-model) for what each check catches.

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
when that app first calls it. Two embedding models are not served here, and each is baked into
its app's image: Visia's SigLIP 2 (`google/siglip2-base-patch16-224`, GPU only, about 0.75 GB of
VRAM) and Vibia's CLAP (`laion/clap-htsat-unfused`, GPU or CPU, about 0.85 GB of VRAM on the GPU).
Both share the GPU with Ollama, so they count against the VRAM left for its models.

Ollama loads models on demand, so apps sharing one model share its memory, while
different models compete for VRAM.
A model is also reloaded whenever a request asks for a different context size,
so every app asks for the same 16,384 tokens and one copy of Qwen3-VL serves
them all.

### GPU residency

The 12 GB GPU holds Luna Socia's chat model (`qwen3:14b`, about 9.7 GB at 8k context)
or the apps' working models (Qwen3-VL 8B with SigLIP 2, CLAP or Docling), not both. The
monitor, which every Ollama call passes through, decides who stays:

- The resident model, `SENTIA_RESIDENT_MODEL`, is asked to stay loaded for good.
- Every other model stays only `SENTIA_GUEST_KEEP_ALIVE` (2 minutes) after its last
  request, whatever the app asked. A request that asks a model to leave (`keep_alive` 0,
  as Socia sends before a picture) passes unchanged.
- Once no model has been used for `SENTIA_RESIDENT_IDLE_SECONDS` (30), ComfyUI is not
  drawing, and the GPU has room, the monitor loads the resident model back at
  `SENTIA_RESIDENT_CONTEXT` (8192, the context Socia asks for; any other would reload it).

The apps' own models leave too: Visia's SigLIP 2, Vibia's CLAP, and Lexia's Docling each
run in a child process that exits after `GPU_MODEL_IDLE_SECONDS` (120) unused
(`common/gpu_worker.py`), which also frees its CUDA context (about 300 MB a process).

So a bulk upload to Visia evicts the resident model once; Qwen3-VL and SigLIP 2 then
process the whole batch side by side, and about two minutes after the last image both
leave and the resident model is back. Measured on the RTX 5070: 10.6 GB with the
resident model, 10.1 GB during a Visia batch, 1.4 GB in between.

Three settings in `.env` tune the server for a 12 GB GPU:

| Variable | Value | Why |
| --- | --- | --- |
| `OLLAMA_FLASH_ATTENTION` | `1` | Needed for a quantized KV cache; less memory per token |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` | Halves the KV cache: Qwen3-VL at 16k takes 6.6 GB instead of 7.8 GB |
| `OLLAMA_NUM_PARALLEL` | `1` | Ollama does not run Qwen3-VL requests in parallel ("model architecture does not currently support parallel requests"), so requests from the apps queue |

### Changing a model

**Last tried:** `qwen3.5:9b` for `qwen3-vl:8b-instruct`, on 2026-11-10. Not adopted: on
Lexia's 89 documents it fixed PF amounts on payslips and ID numbers, but left Form 16
employer PANs, two payslips' net pay and most rent-agreement dates empty. See
`Luna_Lexia/model_evaluations/README.md`.

Before a model replaces another in `SENTIA_MODELS` (and in the apps' `.env` settings that
name it), run these checks. Each app that uses the model has its own evaluation in its
`model_evaluations/` folder: it runs the old and the new model over the same stored items
inside the app's container, through the app's own prompt and settings, without writing to
its database, and scores the two runs. Results hold personal data and stay out of git.

| Check | What it catches | How |
| --- | --- | --- |
| Smoke test | A model that cannot see images, answer in a JSON schema, call a tool, or turn thinking off, or does not fit on the GPU at the apps' context | [`model_checks/check_model.py`](model_checks/check_model.py) `<model>` in the monitor container (about a minute); `--context 8192` for Socia's resident model |
| Lexia's documents | Worse extraction: values not found in the document's text, empty fields, malformed PAN, TAN, GSTIN and IFSC numbers, review flags, crashes, slower documents, and the known regressions of the last change | `Luna_Lexia/model_evaluations/` (`DOCUMENT_MODEL`) |
| Visia's images | Captions and tags that match their image less well in SigLIP 2's space (what meaning search uses), failed replies, captions not in English | `Luna_Visia/model_evaluations/` (`VISION_MODEL`) |
| Vibia's tracks | Captions that match the track's sound less well in CLAP's space, tags the evidence does not support, failed replies | `Luna_Vibia/model_evaluations/` (`CAPTION_MODEL`) |
| Socia's chat | Texting style (length, lists, emoji), saying she is an AI, the wrong language, `create_image` called when it should not be or not called when it should, wrong tool arguments | `Luna_Socia/model_evaluations/` (`LUNASOCIA_CHAT_MODEL`), on a fixed set of made-up conversations |
| Fits beside the others | A model that leaves no room for SigLIP 2, CLAP or Docling during a batch | `ollama ps` shows `100% GPU` while an evaluation runs; the monitor's page shows VRAM; Luna Socia's `qwen3:14b` is resident again afterwards |
| One context size | Ollama reloading a model on every call | The apps all ask for 16k; Socia asks for `SENTIA_RESIDENT_CONTEXT` (8k) |
| Unit tests | Code that breaks with the new setting | Each app's tests in CI; they use fake models, so they say nothing about a model's answers |

The scores cannot judge everything: read the examples each `report.md` lists (the biggest
drops, and every Socia reply) before deciding. Not covered: Fiora's parsing of bank messages
its rules cannot read, and Socia's photo descriptions (`LUNASOCIA_VISION_MODEL`); check a
few by hand after a change.

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

1. **Public address.** Without a tunnel, keep the defaults:
   `SENTIA_AUTH_PUBLIC_URL=http://localhost:19090` and a blank
   `SENTIA_AUTH_COOKIE_DOMAIN`. Every app at `http://localhost:<port>` then shares
   the sign-in, since cookies ignore ports. With a tunnel, route a public hostname
   to `http://localhost:19090`, for example `auth.lunaapps.fyi` as a Public Hostname
   of your Cloudflare tunnel, and set it as `SENTIA_AUTH_PUBLIC_URL`. It must share
   the apps' parent domain, set as `SENTIA_AUTH_COOKIE_DOMAIN`, and each app's
   `LUNA_SSO_URL` and `UI_PUBLIC_URL` change to their public addresses.
2. **Google client.** Use a Google OAuth "Web application" client (one of the
   apps' UI clients works) and add `<SENTIA_AUTH_PUBLIC_URL>/callback` to its
   authorized redirect URIs (`http://localhost:19090/callback` without a tunnel). Set `SENTIA_AUTH_GOOGLE_CLIENT_ID` and
   `SENTIA_AUTH_GOOGLE_CLIENT_SECRET`.
3. **Secrets and accounts.** Set `SENTIA_AUTH_SECRET` (`openssl rand -hex 32`),
   `LUNAAUTH_DB_PASSWORD`, `SENTIA_AUTH_OWNER_EMAIL`, the account that manages
   everyone else at `/admin`, and each app's `LUNA<APP>_API_TOKEN`, equal to that app's,
   so deleting a person reaches its data. Each app can narrow who uses it with `LUNA<APP>_ALLOWED_EMAILS`; `*` allows
   everyone with an account. If the Google client's consent screen is in Testing, add
   each person as a test user there too.
4. **Apps.** In each app's `.env`, set `LUNA_SSO_URL` to the public address and
   `LUNA_SSO_INTERNAL_URL` to `http://luna-sentia-auth:8000`.

For use only on localhost, leave `SENTIA_AUTH_COOKIE_DOMAIN` blank and set
`SENTIA_AUTH_PUBLIC_URL` and every `LUNA_SSO_URL` to `http://localhost:19090`;
browsers share localhost cookies across ports, so the apps still share one
sign-in. Tests: `docker compose run --rm --no-deps -v "$PWD/auth/tests:/workspace/auth/tests" auth python -m unittest discover -s tests -t .`

## Users and separate storage

Every person has an account in Sentia and their own space in every app. The `users`
table in `lunaauth` holds the accounts. The owner (`SENTIA_AUTH_OWNER_EMAIL`, whose id is
always `owner`) adds, disables, and deletes everyone else at `<SENTIA_AUTH_PUBLIC_URL>/admin`.
Disabling someone ends their sessions and connector tokens at once; apps notice within a
minute. Their data is kept.

**Deleting** someone is permanent. It shuts them out, then asks each app to remove their
schema, folder, and download archives, which also ends their public links. The account
goes once every app confirms; until then `/admin` lists the apps still holding data, with
**Retry delete**, so a stopped app cannot silently keep someone's data. Access they gave a
Gmail or Drive connection stays on their Google account until they revoke it there.

**Hiding an app** at `/admin` removes it from everyone but the owner: it leaves Unia's home
page, and its UI and connector refuse them within a minute. Their data stays, and other
apps can still use it, such as Vita storing food photos in Visia. Sentia's own tile on
the home page is shown only to the owner.

| Where | One person's space |
| --- | --- |
| Each app's database | The schema `u_<id>`, created the first time they use the app |
| Visia, Vibia, and Lexia files | The folder `u_<id>` in the app's volume |
| `public` schema | Only the pgvector extension: no tables, so no query can fall through to one |

Each app's backend sets the current person for every request and job, and picks the
schema and folder from that. Nothing has a default: code that does not say who it acts
for raises `NoUserError`.

- **UIs:** the gateway checks the `luna_sso` session, then sends the person's id to its
  backend as `X-Luna-User` beside the app's private token. Browsers cannot set it.
- **MCP connectors:** see below. Each person adds them to their own client.
- **Public links** (`/api/public/...`) work without signing in, by unguessable ID, so
  anyone holding a link can open that file, as before.
- **The home's hardware is the owner's alone.** Only the owner can find or add lights in
  Lumia and play on Vibia's speakers. Lumia's schedules and automations run for the owner
  only, and nobody else can see what the lights or speakers are doing.
- **Shared and first-come:** everyone shares Ollama, which runs one model request at a
  time, so analysis can queue. There are no per-person storage limits yet.

To check or back up one person: `\dn` in `psql` lists schemas, and
`pg_dump -n u_<id>` dumps one. Each person's tables are in their schema, and the owner's in `u_owner`, so manual SQL names it, as in `SELECT * FROM u_owner.lights`.

### Hidden-items PIN

Visia, Lexia, and Vibia keep hidden images, documents, and tracks out of every gallery,
search, and MCP tool. Their Hidden tabs also ask for a PIN, so a signed-in browser left
open, or a copied session cookie, is not enough to open them. Each person has one PIN for
all three apps, which they set at `<SENTIA_AUTH_PUBLIC_URL>/pin`, linked from the sign-in
page and from each Hidden tab.

- **Setting or forgetting it:** setting a first PIN, or a new one without the old one,
  needs a Google sign-in from the last 10 minutes; the page offers one. Changing it
  otherwise needs the current PIN. Hidden items are never deleted either way.
- **Guessing:** a PIN is 6 to 12 digits, stored only as a salted PBKDF2 hash in
  `hidden_pins`. Five wrong PINs lock it for 15 minutes, and after that each wrong one
  locks it again until a right one is typed. Every attempt is counted before it is
  checked, so parallel guesses cannot get past the limit.
- **In the apps:** typing the PIN unlocks that app's hidden items for this sign-in only,
  until the person leaves the tab, presses **Lock**, or stays idle for 10 minutes (an
  hour at most). Until then the gateway also refuses a hidden item's files and
  thumbnails, and it never lets the browser cache them.
- **Deleting someone** removes their PIN with their account.

**Starter images:** Luna Visia can give everyone the images the owner chooses, as their own copy; see its [Starter library](https://github.com/pranjallk1995/Luna_Visia#starter-library).

### Connecting an MCP client

Add a server's address, such as `https://vita-mcp.lunaapps.fyi/mcp`, as a custom connector.
The client registers itself, sends the person to sign in with Google, and shows an
approval page; the tools then act on that person's data alone. An MCP server without a
valid token answers 401, and a token is only valid on the server it was issued for.

| Route | Purpose |
| --- | --- |
| `/.well-known/oauth-authorization-server` | How clients find the routes below |
| `/oauth/register` | A client registers itself (RFC 7591) |
| `/oauth/authorize` | The person signs in and approves the client (PKCE required) |
| `/oauth/token` | Codes and refresh tokens become access tokens (one hour; refresh tokens last 30 days and rotate) |
| `/oauth/introspect` | For MCP servers: whose access token this is |
| `/admin` | The owner adds and disables people |
| `/pin` | Each person sets or changes their hidden-items PIN |
| `/pin/status`, `/pin/verify` | For Visia, Lexia, and Vibia: whether a PIN is set, and whether a typed one is right |

## Image model

ComfyUI serves Qwen-Image 2.1 Turbo (bf16, with the int8 Qwen3-VL 8B text encoder) to
the apps that make pictures, today Luna Socia. `comfyui-pull` downloads its files (about
24 GB) into `luna_sentia_comfyui_models` once. It has no sign-in of its own, so it has no
host port: the apps call it on the private network. The GPU holds Ollama's models or ComfyUI's, not
both: an app unloads Ollama's models before a picture and asks ComfyUI to free its own
after (`POST /free`), so the next model call after a picture first reloads its model.

Its own page, the node editor, is for the owner only. The model monitor serves it on
`127.0.0.1:19082` behind Luna's sign-on (`monitor/app/comfyui_gate.py`): like the monitor's
page, it checks the `luna_sso` cookie, then passes every request and ComfyUI's websocket
(live progress and previews) through, without the cookie. Requests from other sites are
refused. With a tunnel, route a hostname under `SENTIA_AUTH_COOKIE_DOMAIN` to
`http://localhost:19082` (for example `comfyui.lunaapps.fyi`) and set it as
`SENTIA_COMFYUI_PUBLIC_URL`; Luna Unia's Sentia card links to it (`LUNASENTIA_COMFYUI_URL`
in Unia's `.env`). A picture queued there by hand loads Qwen-Image like one from Socia and
pushes the chat model out; press "Unload models" when done, so the monitor can bring the
resident model back.

## Model monitor

Every app still calls `http://luna-sentia-ollama:11434`. That name belongs to the
monitor, which passes each request to Ollama at `luna-sentia-ollama-engine`
unchanged and times it on the way back. It never stores prompts, images, or
replies, only their sizes, token counts, and timings. Its page shows, live and over
stored history:

- **Each model call**, split into waiting (queued behind other requests, as Ollama runs
  Qwen3-VL one at a time), loading, reading the input, and writing the output, with
  token counts and speeds, on a timeline with one row per app.
- **The GPU**: utilisation, memory bandwidth, VRAM (all of it, and the share Ollama's
  models hold), power, temperature, and clocks, every second. Under WSL the driver
  cannot split VRAM by process, so the total includes Visia's SigLIP, Lexia's Docling,
  Vibia's CLAP, and Windows.
- **Requests in flight and waiting**, and the models Ollama has loaded.
- **Models outside Ollama**, which three apps run themselves on the same GPU: Visia's
  SigLIP 2 (image and text embeddings), Lexia's Docling (layout, table-structure, and
  OCR models that read each document), and Vibia's CLAP (which hears each track). Their
  work never passes through the monitor, so each app counts it and reports it at
  `GET /api/models`, for the owner only. Every ten seconds the monitor asks, with the
  app's `LUNA<APP>_API_TOKEN` from this `.env`, and shows whether each model is loaded
  and where, its calls and busy time since the app started, and the GPU memory the
  app's process holds.
- **Signals for a batching server such as vLLM**: how often requests overlap, how much
  of their time is spent waiting, and how much VRAM stays free. The page's Guide
  explains each number.

The data goes into its own `lunamonitor` database: `gpu_samples` (five-second
averages and peaks, kept `MONITOR_SAMPLE_DAYS`, 30 by default), `model_calls` (kept
`MONITOR_CALL_DAYS`, 365), and `model_events` (model loads and unloads).

The page is for the owner only and signs in through Sentia, so like the apps it must
be reached at an address under `SENTIA_AUTH_COOKIE_DOMAIN`. Route
`SENTIA_MONITOR_PUBLIC_URL` (such as `https://monitor.example.com`) to
`http://localhost:19081` in your tunnel. An app can name itself in an `X-Luna-App`
header; otherwise the monitor names it from its container.
The page names its stylesheet and script by their contents, so an update shows at once,
without a hard refresh, even through Cloudflare's cache.

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

The model monitor's database is `lunamonitor`, with `LUNAMONITOR_DB_PASSWORD`.
Username `sentia` with `SENTIA_POSTGRES_PASSWORD` sees every database.

## Operations

- **Stopping:** stop the apps first, then Sentia (`../luna.sh stop` does both in
  order). `docker compose down` stops PostgreSQL and Ollama but keeps their
  volumes; the network is removed only when no Luna app is still attached.
- **Privacy:** PostgreSQL and Ollama have no host port; only containers on
  `luna_sentia_network` can reach them. Adminer, the sign-on service, and the model
  monitor's page listen only on localhost; publish the sign-on service and the
  monitor through your tunnel.
- **Adding an app:** add it to the loop in `postgres/init-databases.sh` and its
  `LUNA<APP>_DB_PASSWORD` to `.env`, `.env.example`, and the `postgres-init`
  service, run `postgres-init`, then join the external network in the app's
  Compose file with a unique alias per service. Name them `luna-<app>-ui` and
  `luna-<app>-mcp` and add the app to `LUNA_APPS` in Luna Unia's
  `ui/config.py`, so Unia finds it and links to it:

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
