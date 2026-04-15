# Shorts Publishing Studio

An MVP full-stack app for turning uploaded videos into image-driven YouTube Shorts packages with Gemini vision, then publishing them through the YouTube Data API.

The project is split into:

- `frontend/`: a Next.js studio where creators connect YouTube, upload a video, review generated metadata, and publish the video.
- `backend/`: a FastAPI service that accepts uploads, samples key frames from the video, sends those images to the Gemini API, stores the upload temporarily, and publishes it to YouTube through the official API.

## What ships in this version

- Video upload flow focused on image-only analysis
- FastAPI upload endpoint with multipart handling
- Video metadata extraction with OpenCV and ffprobe fallback
- Frame extraction every few seconds from the uploaded video
- Gemini vision analysis using sampled frame images
- Structured outputs for hook titles, descriptions, hashtags, detected objects, and frame insights
- Google OAuth connection for YouTube publishing
- Temporary upload sessions that are deleted after successful YouTube upload
- Regenerate flow and editable publish form in the frontend

## Project structure

```text
.
├── backend
│   ├── app
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── prompts.py
│   │   ├── schemas.py
│   │   └── services
│   │       ├── pipeline.py
│   │       ├── video.py
│   │       ├── vision.py
│   │       └── youtube.py
│   ├── requirements.txt
│   └── tests
└── frontend
    ├── app
    │   ├── globals.css
    │   ├── layout.tsx
    │   └── page.tsx
    ├── components
    │   └── upload-studio.tsx
    ├── lib
    │   ├── api.ts
    │   └── types.ts
    ├── package.json
    ├── next.config.mjs
    └── tsconfig.json
```

## Run the backend

```bash
cd /Users/rohithreddy/Documents/New\ project/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload
```

The API starts on `http://localhost:8000`.

Create `backend/.env` or export variables in your shell:

```bash
export GEMINI_API_KEY=your_gemini_api_key_here
export GEMINI_VISION_MODEL=gemini-2.5-flash-lite
export GEMINI_FALLBACK_MODELS=
export VIDEO_UPLOAD_DIR=storage/uploads
export OAUTH_SESSION_DIR=storage/oauth_sessions
export FRAME_SAMPLE_SECONDS=3
export CORS_ORIGINS=http://localhost:3000
export FRONTEND_BASE_URL=http://localhost:3000
export GOOGLE_CLIENT_ID=your_google_client_id_here
export GOOGLE_CLIENT_SECRET=your_google_client_secret_here
export GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/youtube/callback
export BROWSER_SESSION_COOKIE_SAMESITE=lax
export BROWSER_SESSION_COOKIE_SECURE=false
```

## Run the frontend

```bash
cd /Users/rohithreddy/Documents/New\ project/frontend
npm install
npm run dev
```

The frontend starts on `http://localhost:3000`.

Optional frontend environment variable:

```bash
export NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

## How the backend is designed

The pipeline is intentionally split into visual and publish stages:

1. `video.py` saves the upload into a temporary session, reads metadata, and extracts sampled frames.
2. `vision.py` converts those images to Gemini image parts and sends them to `generate_content`.
3. The model returns structured visual analysis containing objects, frame insights, hook titles, descriptions, and hashtags.
4. `pipeline.py` assembles that result with local video metadata and a temporary upload session ID.
5. `youtube.py` handles Google OAuth and uploads the video plus metadata to YouTube.

## Notes

- After changing backend requirements, run `pip install -r requirements.txt` again inside `backend/.venv`.
- The output is intentionally based on visible frame evidence only. It does not use transcript, speech, or creator notes.
- Uploads are stored under `backend/storage/uploads` only temporarily and are deleted after a successful YouTube upload or session expiry.
- OAuth session records are stored server-side under `backend/storage/oauth_sessions` by default.
- Install `ffmpeg` on the backend machine if you want to use the pre-upload enhancement options such as Pop Look or Audio Cleanup.
- Gemini's official docs currently list a free tier for the Gemini Developer API, with lower limits than paid usage.
- If the backend returns an error on generate, check that `GEMINI_API_KEY` is set and valid.
- `GEMINI_FALLBACK_MODELS` accepts a comma-separated list of backup Gemini model IDs. The backend will try them only when the primary model fails with quota or rate-limit errors.
- If the YouTube publish flow fails, check that the Google OAuth client is configured and that the redirect URI in Google Cloud matches `GOOGLE_REDIRECT_URI`.

## Production deployment

The simplest non-Oracle backend for this project is Render. The repo now includes:

- `render.yaml`
- `backend/Dockerfile`
- `backend/.env.render.example`

Render is a better fit than serverless functions here because the backend needs multipart uploads, temporary local file handling, OpenCV, and `ffmpeg` for optional pre-upload enhancements.

### Deploy the backend on Render

1. In Render, click `New +` and choose `Blueprint`.
2. Connect the GitHub repo `Rohith7495/shorts-publishing-studio`.
3. Render will detect `render.yaml` and create a web service named `shorts-publishing-studio-api`.
4. During setup, enter your secret values for:
   - `GEMINI_API_KEY`
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
5. After the first deploy, open the Render service and copy its public URL.
6. If the hostname is not exactly `https://shorts-publishing-studio-api.onrender.com`, update:
   - `GOOGLE_REDIRECT_URI` in Render
   - `NEXT_PUBLIC_API_BASE_URL` in Vercel
   - the Google OAuth redirect URI in Google Cloud

Use `backend/.env.render.example` as the reference for the Render environment variables.

In Vercel, set:

```bash
NEXT_PUBLIC_API_BASE_URL=https://shorts-publishing-studio-api.onrender.com
```

In Google Auth Platform, set:

- Authorized JavaScript origin: `https://shorts-publishing-studio.vercel.app`
- Authorized redirect URI: `https://shorts-publishing-studio-api.onrender.com/api/auth/youtube/callback`

Important notes for Render free web services:

- Free services can spin down after inactivity, so the first request after idle can be slow.
- This backend stores temporary uploads and OAuth session files on the local filesystem only. If the Render instance restarts, you may need to reconnect YouTube.
- Uploaded videos are still deleted after successful YouTube upload, just like the local version.

### Oracle alternative

If you want an always-on VM later, the repo still includes ready-to-copy Oracle deployment templates:

- `deploy/oracle/shorts-backend.service`
- `deploy/oracle/Caddyfile`
- `backend/.env.production.example`

Use them like this on your Oracle VM:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git ffmpeg caddy
sudo mkdir -p /opt
sudo chown "$(whoami)":"$(whoami)" /opt
git clone <your-repo-url> /opt/shorts-publishing-studio
cd /opt/shorts-publishing-studio/backend
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.production.example .env
```

Edit `backend/.env` and replace:

- `https://shorts-publishing-studio.vercel.app`
- `https://rohithshortsapi.duckdns.org`
- `your_google_client_id_here`
- `your_google_client_secret_here`
- `your_gemini_api_key_here`

Then install the backend service:

```bash
sudo cp /opt/shorts-publishing-studio/deploy/oracle/shorts-backend.service /etc/systemd/system/shorts-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now shorts-backend
sudo systemctl status shorts-backend
```

Then install the Caddy config:

```bash
sudo cp /opt/shorts-publishing-studio/deploy/oracle/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

The included `Caddyfile` is already set to `rohithshortsapi.duckdns.org`. If you change domains later, update that hostname before you reload Caddy.

In Vercel, deploy `frontend/` and set:

```bash
NEXT_PUBLIC_API_BASE_URL=https://rohithshortsapi.duckdns.org
```

In Google Auth Platform, add the matching production values:

- Authorized JavaScript origin: `https://shorts-publishing-studio.vercel.app`
- Authorized redirect URI: `https://rohithshortsapi.duckdns.org/api/auth/youtube/callback`
