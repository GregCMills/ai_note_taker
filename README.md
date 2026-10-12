# AI Note Taker

Talk, and it writes your notes. You think out loud on your phone or computer, and a local LLM turns what you say into tidy, organised markdown notes as you go: merging related points, fixing earlier ones when you change your mind, and keeping each project's notes in tabs.

By default everything runs on your own machine: speech-to-text and the LLM are both local, and nothing is sent to a cloud service. You can also run just the app and point it at LLM and speech-to-text services elsewhere.

## What it does

- **Live notes from speech.** Tap the mic and talk. Your words appear as you speak, and after each pause the AI folds them into the notes.
- **Projects and tabs.** Each project has its own notes, split into tabs. Talk to the project manager (with no project open) to create, open, rename, delete or restore projects by voice.
- **Steer it by talking.** Say how you want notes taken ("keep to-dos at the top") and it remembers that for the project. You can also edit notes by hand.
- **History and undo.** Every change is saved as a version, so you can revert a change or restore an earlier state.
- **Accounts.** Each person signs in and gets their own private projects. Accounts are created from the command line; there is no public sign-up.
- **Works on phones.** The layout is mobile first, and it can be added to the home screen.

## Requirements

- Linux with **Docker** and Docker Compose.
- **Two NVIDIA GPUs** and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html):
  - GPU 0 runs the app and speech-to-text ([IBM Granite Speech 4.1 2B](https://huggingface.co/ibm-granite/granite-speech-4.1-2b)).
  - GPU 1 runs the LLM, with a 40k-token context and about 20 GB of VRAM. Two setups are included, both Unsloth Q4_K_XL quantisations on the standard [llama.cpp](https://github.com/ggml-org/llama.cpp) server image:
    - [Qwen 3.8 27B](https://huggingface.co/unsloth/Qwen3.8-27B-GGUF): slower (about 55 tokens/s) but more careful with edits.
    - [Gemma 4 26B-A4B](https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF): fast (about 125 tokens/s), less careful with edits.
- Disk space for the models. They download into `~/.cache/huggingface` on first start, which takes a while.

No GPU? See [App only](#app-only-llm-and-speech-to-text-elsewhere) to use an LLM and speech-to-text service running somewhere else.

## Quick start

```bash
git clone https://github.com/GregCMills/ai_note_taker.git
cd ai_note_taker
./start.sh qwen
```

Use `./start.sh gemma` (or just `./start.sh`) for the Gemma setup instead. `start.sh` builds the web app, starts the app and the LLM, prints the address to open, and shows the logs. **Ctrl+C stops everything.** The models take a minute or two to load after the address appears.

Then create your account, in a second terminal:

```bash
docker compose -f docker-compose.qwen.yml exec app /venv/bin/python -m server.accounts add <name>
```

(With Gemma, use `docker-compose.gemma.yml` in this and the other commands below.) It asks for a password (at least 10 characters). Open the printed address, for example `https://192.168.1.20:8443`, and sign in.

### The certificate warning

Browsers only allow the microphone on HTTPS pages, so the app makes its own self-signed certificate on first start. Your browser will warn that the connection isn't private; choose **Advanced → Proceed** to continue.

To include your machine's LAN address or hostname in the certificate, create a `.env` file next to the compose files:

```
TLS_HOSTS=192.168.1.20,my-pc
```

Then delete `data/certs/` and restart, so a new certificate is made.

## App only (LLM and speech-to-text elsewhere)

The app can run on its own, on any machine with Docker (no GPU), and reach the LLM and speech-to-text over the OpenAI API. They can be hosted services (OpenAI, Groq, OpenRouter, ...) or your own servers (llama.cpp's `llama-server`, vLLM, Ollama, [speaches](https://github.com/speaches-ai/speaches), ...).

Put the services in `.env`:

```
LLM_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-...
LLM_MODEL=qwen/qwen3.8-27b
STT_URL=https://api.groq.com/openai/v1
STT_API_KEY=gsk_...
STT_MODEL=whisper-large-v3-turbo
```

Then start it, and create accounts as above using `docker-compose.api.yml`:

```bash
./start.sh api
```

- The LLM must support tool calling (function calling); the AI edits notes through tools.
- For a service on the same machine, use `host.docker.internal` as its host, e.g. `http://host.docker.internal:8080/v1`.
- Hosted services charge per request, so two features are off by default in this mode:
  - `LIVE_TRANSCRIPT`: showing words while you are still speaking, which re-sends the sentence about once a second.
  - `LLM_WARMUP`: having the LLM read the next prompt ahead of time after each turn.

  If your services are self-hosted, set both to `1` in `.env` for live words and faster turns.
- Other optional settings: `STT_LANGUAGE` (default `en`; empty to auto-detect) and `LLM_CONTEXT` (your model's context size, shown in the agent inspector).

## Managing accounts

Run these while the app is running:

```bash
docker compose -f docker-compose.qwen.yml exec app /venv/bin/python -m server.accounts add <name>
docker compose -f docker-compose.qwen.yml exec app /venv/bin/python -m server.accounts passwd <name>
docker compose -f docker-compose.qwen.yml exec app /venv/bin/python -m server.accounts remove <name>
docker compose -f docker-compose.qwen.yml exec app /venv/bin/python -m server.accounts list
```

- `passwd` changes a password and signs that person out on every device.
- `remove` deletes the account but keeps their notes in `data/users/<name>/`.
- Signing in stays valid for 30 days per device. The person icon in the top bar shows who is signed in and has **Sign out**.
- Repeated wrong passwords lock out that address or account for 15 minutes.

Everyone signed in to one account shares its projects, so give each person their own account.

## Your data

Everything lives in `data/`, which is ignored by git:

| Path | What it holds |
| --- | --- |
| `data/users/<name>/` | That person's projects (each a git repo of markdown notes), transcripts, AI memory and settings |
| `data/accounts.json` | Accounts and password hashes |
| `data/session.key` | The key that signs sign-in cookies; deleting it signs everyone out |
| `data/certs/` | The self-signed HTTPS certificate |

Back up `data/` to keep your notes.

## Settings

Most behaviour can be changed in the app under **⋯ → Settings**: the microphone each device uses, how long a pause ends a sentence, the system prompt that shapes the notes, and per-project instructions.

Server settings are environment variables in `compose.app.yml`, for example:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASR_ENGINE` | `granite` | Speech-to-text: `granite` (more accurate), `whisper` (faster-whisper large-v3-turbo), or `api` (see [App only](#app-only-llm-and-speech-to-text-elsewhere)) |
| `WHISPER_LANGUAGE` | `en` | Language for Whisper; empty to auto-detect |
| `LLM_API_KEY` | `test` | Key shared between the app and the LLM container (not exposed outside Docker) |

## Development

The server is FastAPI (`server/`) and the frontend is React + Vite (`web/`). The server code is mounted into the container, so restarting the `app` container picks up changes:

```bash
docker restart notetaker-app
```

For live frontend reloading, run the Vite dev server against the running app:

```bash
cd web
npm install
npm run dev
```

It proxies `/api` and `/ws` to `https://localhost:8443` (override with `BACKEND=...`).

## Troubleshooting

- **The mic button is red but no text appears.** The wrong microphone is probably selected. Pick the right one under **⋯ → Settings → Microphone**. The app log shows each recording's peak level, and a level near 0% means silence arrived:
  `docker logs -f notetaker-app`
- **"LLM unavailable" in the menu.** The LLM is still loading, or failed to start: `docker logs -f notetaker-llm`.
- **Full screen does nothing in Edge on Android.** Edge ignores full-screen requests from the page. Use Chrome, or add the page to your home screen.
