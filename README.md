# GrabitPro Backend

A tiny Flask + yt-dlp API that powers the [GrabitPro](https://grabitpro.lovable.app) frontend.
Extracts direct download URLs for Instagram / YouTube / TikTok / etc.

## Endpoints

| Method | Path      | Description                              |
| ------ | --------- | ---------------------------------------- |
| GET    | `/`       | Health check                             |
| POST   | `/info`   | `{ "url": "..." }` → metadata + formats  |
| GET    | `/stream` | `?u=<direct-url>&name=<file>` passthrough |

All endpoints except `/` require `Authorization: Bearer $API_SECRET`.

## Deploy on Railway

1. Push this repo to GitHub (already done → `RahulXDind/GrabitPro`).
2. On [Railway](https://railway.app) → **New Project** → **Deploy from GitHub repo** → pick `GrabitPro`.
3. Railway auto-detects the `Dockerfile`.
4. Under **Variables**, add:
   - `API_SECRET` — any long random string. Use the same value in the Lovable frontend secret `YTDLP_API_SECRET`.
5. Under **Settings → Networking**, click **Generate Domain**. Copy the HTTPS URL (e.g. `https://grabitpro-production.up.railway.app`) into the Lovable frontend secret `YTDLP_API_URL`.
6. Done — hit `https://<your-domain>/` and you should see `{"ok": true, ...}`.

## Local dev

```bash
pip install -r requirements.txt
export API_SECRET=devsecret
python app.py
# → http://localhost:8000
```

Test:

```bash
curl -X POST http://localhost:8000/info \
  -H "Authorization: Bearer devsecret" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
```

## Notes

- `ffmpeg` is baked into the Docker image (yt-dlp needs it for many sites).
- The `/info` response filters to **progressive** formats (video + audio in one file) so the frontend can offer a direct one-click download without merging.
- If a site blocks hotlinking from the browser, the frontend can route the download through `/stream` instead.
