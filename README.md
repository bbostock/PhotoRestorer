# PhotoRestorer

Local browser app for restoring historical family photos with Gemini image editing.

## Features

- server-side folder browser for selecting photo folders on the host machine
- one-folder-at-a-time workflow
- manual restore or timed automatic sequential processing
- face and pose retention enforced as hard prompt constraints
- optional colorization toggle
- output naming beside originals as `_r01`, `_r02`, and so on
- primary and secondary reference uploads
- temporary compare previews stored outside the repo
- localhost and LAN access

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

You can either:

- set `GOOGLE_API_KEY` on the server, or
- leave the server without a key and let each user enter their own Gemini API key in the app

## Run

```bash
python3 family_restore_server.py
```

Then open:

- `http://127.0.0.1:8765/family_restore_gui.html`
- or the printed LAN URL from another device on your network

## Usage

1. Click `Browse Server Folders`.
2. Choose the photo folder on the server.
3. Upload reference images if needed.
4. Adjust prompt, extra note, colorization, overwrite, and automatic processing settings.
5. Use `Restore Selected` for manual work, or `Start Automatic Processing` when pause is greater than `0`.

## Output behavior

- Restored files are written beside the originals in the selected folder.
- Names use the pattern `<stem>_rNN.png`.
- When `Overwrite latest _rNN` is on, the most recent restore is replaced.
- When it is off, the next `_rNN` file is created.

## Notes

- Runtime settings are stored in `family_restore_prompt_config.json`, which is intentionally git-ignored.
- Token usage logs are written under `logs/`.
- Temporary reference uploads and compare previews are stored in the system temp directory, not in this repo.
- The Gemini API key field is stored only in each user's browser, not in the shared server config.
