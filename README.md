# PhotoRestorer

PhotoRestorer now has two deployment targets:

- `family_restore_server.py`: local/LAN standalone server for your own server-folder workflow
- `passenger_wsgi.py` + `family_restore_hosted_wsgi.py`: hosted upload-only app for cPanel/Passenger

Both variants use the same [family_restore_gui.html](/Users/bbostock/PhotoRestorer/family_restore_gui.html).

## Python packages

Install the Python packages from [requirements.txt](/Users/bbostock/PhotoRestorer/requirements.txt) into a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The only Python packages currently required are:

- `google-genai`
- `Pillow`

## Local app

Use this when you want to browse folders on your own machine or LAN server and save restored files beside the originals.

Run:

```bash
python3 family_restore_server.py
```

Then open:

- `http://127.0.0.1:8765/family_restore_gui.html`
- or the printed LAN URL

Local behavior:

- server-side folder browser
- one folder at a time
- restored files saved beside originals as `_r01`, `_r02`, and so on
- settings persisted in `family_restore_prompt_config.json`

## Hosted cPanel app

Use this when the app is hosted for other users. In this mode:

- users upload their own target photos
- users upload their own same-person reference photos
- users provide their own Gemini API key in the browser
- no browsing of your server folders
- uploaded files live only in a temporary server-side session area
- restored results are downloaded individually

### HostPresto / cPanel values

These are the values that worked with HostPresto's CloudLinux/LiteSpeed setup:

- `Python version`: `3.12.12` or the exact Python 3 version you intend to keep
- `Application root`: `public_html/photorestorer`
- `Application URL`: `bostock.com / photorestorer`
- `Application startup file`: `passenger_wsgi.py`
- `Application Entry point`: `application`

Important:

- create the app with the final Python version from the start
- do not create it as Python 2.7 and then switch it later
- cPanel may generate its own initial `passenger_wsgi.py`; after creation, replace it with [passenger_wsgi.py](/Users/bbostock/PhotoRestorer/passenger_wsgi.py)

### Files to upload into the hosted app root

Upload these files into `/home/<cpanel-user>/public_html/photorestorer`:

- [passenger_wsgi.py](/Users/bbostock/PhotoRestorer/passenger_wsgi.py)
- [family_restore_hosted_wsgi.py](/Users/bbostock/PhotoRestorer/family_restore_hosted_wsgi.py)
- [family_restore_server.py](/Users/bbostock/PhotoRestorer/family_restore_server.py)
- [family_restore_gui.html](/Users/bbostock/PhotoRestorer/family_restore_gui.html)
- [requirements.txt](/Users/bbostock/PhotoRestorer/requirements.txt)

Do not upload `family_restore_prompt_config.json` for the hosted app.

### HostPresto deployment order

1. Create the Python app in cPanel with the values above.
2. Wait for cPanel to create the app root and virtualenv.
3. Upload the files listed above into `public_html/photorestorer`.
4. If cPanel generated a starter `passenger_wsgi.py`, replace it with the repo copy.
5. Install the Python packages from `requirements.txt`.

If the cPanel `Run Pip Install` button is silent or unreliable, use SSH instead:

```bash
source /home/<cpanel-user>/virtualenv/public_html/photorestorer/3.12/bin/activate
cd /home/<cpanel-user>/public_html/photorestorer
pip install -r requirements.txt
```

6. Start the app from cPanel.
7. Open the hosted URL.

### Required `.htaccess` Passenger mapping

On HostPresto, the Python app did not always write the Passenger directives correctly. If the site shows a directory listing instead of the app, make sure `/home/<cpanel-user>/public_html/photorestorer/.htaccess` contains:

```apache
PassengerAppRoot "/home/<cpanel-user>/public_html/photorestorer"
PassengerBaseURI "/photorestorer"
PassengerPython "/home/<cpanel-user>/virtualenv/public_html/photorestorer/3.12/bin/python"

<IfModule LiteSpeed>
</IfModule>
```

Then restart Passenger:

```bash
mkdir -p /home/<cpanel-user>/public_html/photorestorer/tmp
touch /home/<cpanel-user>/public_html/photorestorer/tmp/restart.txt
```

### Debugging HostPresto failures

If the hosted app returns `503`, the first file to inspect is:

```bash
tail -100 /home/<cpanel-user>/public_html/photorestorer/stderr.log
```

Two failure modes were seen during setup:

- stale Python 2.7 wrapper state after changing Python versions
- missing Passenger directives in `.htaccess`, which caused LiteSpeed to serve a directory listing instead of the app

### Hosted Gemini key behavior

For the hosted app, the recommended setup is:

- leave `GOOGLE_API_KEY` unset on the server
- each user enters their own Gemini API key in the app

The Gemini API key field is stored only in that user's browser and is sent only with that user's restore requests.

## Notes

- Temporary reference uploads, hosted target uploads, and compare previews are stored in the system temp directory, not in this repo.
- Token usage logs are written under `logs/`.
- The shared UI now uses path-safe API URLs, so it can run under a cPanel subpath like `/photorestorer`.
