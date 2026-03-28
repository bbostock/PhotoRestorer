# Family Photo Restoration Project Spec

## Purpose

Build a new local browser-based restoration app for family photos, based on the existing NanoBanana and local restore tooling already used in this workspace.

The new project should:

- restore old family photos using Gemini image editing
- support one-photo-at-a-time review and restoration
- allow optional reference images for face, hair, clothing, or color guidance
- preserve originals and save restored outputs separately
- persist prompt settings and uploaded references
- provide a simple browser GUI for non-technical use

## Existing Source Code To Reuse

These are the main files to copy or adapt.

### Local restore app in this project

- Server/API: `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_server.py`
- Browser GUI: `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_gui.html`

### Existing NanoBanana project

- Project overview: `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/README.md`
- Gemini/Gradio implementation: `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/nano_banana_image_edit.py`
- Prompt/config example: `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/nano_input.json`

## Recommended Project Structure

Create a new project with a structure like this:

```text
family-photo-restore/
  README.md
  requirements.txt
  family_restore_server.py
  family_restore_gui.html
  family_restore_prompt_config.json
  family_restore_source_overrides.json
  images/
    input/
    restored/
    restored_previews/
    reference_uploads/
    source_overrides/
  logs/
```

## Core User Workflow

1. Start a local Python server.
2. Open the browser GUI.
3. Browse and select a family photo from the input folder.
4. Optionally upload one or two reference photos.
5. Edit the restoration prompt if needed.
6. Add a photo-specific note if needed.
7. Run restore.
8. Review original, restored image, and compare preview.
9. Keep the restored output and move on to the next photo.

## Functional Requirements

### Image source handling

- Scan a configured folder for source images.
- Support `.png`, `.jpg`, and `.jpeg`.
- Prefer `.png` over `.jpg` where appropriate.
- Allow alternate-source override per image, so a problematic original can be replaced with another local file.

### Reference image handling

- Support a primary reference image.
- Support a secondary reference image.
- Allow upload of references from anywhere on disk.
- Persist last-used references across refreshes.

### Prompt/config handling

- Store a default restoration prompt in JSON.
- Allow live editing of the prompt in the browser.
- Persist the last-used prompt.
- Allow a per-image extra note.
- Persist the last-used extra note and option toggles.

### Restore output handling

- Never overwrite original images.
- Save restored outputs to a dedicated folder.
- Save compare-strip previews to a dedicated folder.
- Save restored outputs as PNG.
- Support a minimum output width setting.
- Keep aspect ratio and general framing consistent.

### GUI requirements

- Left pane: scrollable image list with search/filter.
- Right pane: current image settings, prompt, notes, references, preview.
- Preview area should show:
  - original
  - restored image
  - compare strip
- GUI should display success/failure messages clearly.

## Suggested Output Naming

- Restored image:
  - `images/restored/edited_<source-stem>.png`
- Compare preview:
  - `images/restored_previews/compare_<source-stem>.png`

## Persistence Files

Recommended config files:

- `family_restore_prompt_config.json`
  - prompt text
  - primary reference image path
  - secondary reference image path
  - extra note
  - loosen-constraints flag
  - minimum output width
- `family_restore_source_overrides.json`
  - per-source-image override mapping

## Technical Architecture

### Frontend

- One static HTML page with vanilla JavaScript.
- Use fetch calls to communicate with local API endpoints.
- Reuse structure and interaction style from:
  - `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_gui.html`

### Backend

- Lightweight Python HTTP server.
- Reuse and adapt the API pattern from:
  - `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_server.py`

### Gemini integration

- Use the NanoBanana Gemini logic as the starting point:
  - `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/nano_banana_image_edit.py`
- Reuse prompt/config ideas from:
  - `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/nano_input.json`

## API Endpoints To Mirror

The new project should have equivalents of these local endpoints:

- `GET /api/images`
- `GET /api/config`
- `POST /api/config`
- `POST /api/reference-upload`
- `POST /api/source-override-upload`
- `POST /api/source-override-clear`
- `POST /api/restore`

These endpoint patterns already exist in:

- `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_server.py`

## Recommended Default Prompt Behavior

For family-photo restoration, the default prompt should focus on:

- restoring damage, fading, and discoloration
- preserving the original composition and pose
- preserving facial identity carefully
- using references only as attribute/style guidance
- avoiding invented objects or modern elements
- optionally supporting either:
  - cleanup-only restoration
  - cleanup plus colorization

Suggested baseline prompt:

```text
Task: High-fidelity family photo restoration.

Restore the target photograph carefully while preserving the original composition, pose, facial identity, and historical character of the image.

If reference images are provided, use them only for identity, hair, clothing, age cues, and color guidance. Do not copy pose or framing from the references.

Repair fading, scratches, stains, tears, discoloration, blur, and age damage where possible. Keep the result natural and believable. Do not invent props, jewelry, scenery, modern clothing details, or extra people.

If colorization is requested, apply realistic, restrained color consistent with the period and the references.
```

## Environment Requirements

Recommended setup:

- Python virtual environment
- `GOOGLE_API_KEY` environment variable
- Python packages similar to NanoBanana:
  - `google-genai`
  - `pillow`
  - `gradio` only if a Gradio version is also desired

NanoBanana setup reference:

- `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/README.md`

## Notes On Scope

This new project should intentionally omit:

- assignment-tool logic
- form/poster generation
- animation/manifest building

It should focus only on restoration workflow for still family photographs.

## Best Reuse Strategy

### Reuse almost directly

- `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_gui.html`
  - for the local browser interface
- `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/IWKA/Wu Taijichuan/WuShortFormApp/image_restore_server.py`
  - for the local HTTP API, config persistence, uploads, and preview generation

### Reuse selectively

- `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/nano_banana_image_edit.py`
  - for Gemini request logic, prompt handling, and output saving patterns
- `/Users/bbostock/Library/CloudStorage/Dropbox/My Documents/Bill/NanoBanana/nano_input.json`
  - for config structure and generation settings

## Suggested Deliverables For The New Project

Minimum first version:

- `family_restore_server.py`
- `family_restore_gui.html`
- `README.md`
- `requirements.txt`
- working local restore flow using Gemini
- persisted prompt and references
- restored PNG outputs
- compare preview generation

Second iteration:

- batch queue mode
- album/project presets
- optional colorization mode
- optional face-specific reference workflow
