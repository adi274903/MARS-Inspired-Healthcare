# EvalBot front end

Dependency-free chat UI for a healthcare-AI evaluation assistant.

## Run
```bash
python3 -m http.server 8000
# open http://localhost:8000
```

## Connect your back end
Runs in demo mode with canned answers. To go live, set the endpoint before `app.js` loads (e.g. in `index.html`):

```html
<script>window.EVALBOT_CONFIG = { API_URL: "https://your-host/api/chat" };</script>
```

Request: `POST { messages: [{role: "user"|"assistant", text}], context: {system, stage, region, focus: []} }`

Response (JSON):
```json
{
  "text": "markdown string (## headings, lists, **bold**, > quotes supported)",
  "checklist": { "title": "...", "groups": [{ "name": "...", "items": ["..."] }] },
  "citations": [
    { "type": "arxiv|pubmed|web", "title": "...", "authors": "...", "venue": "...", "year": 2023,
      "arxiv": "2307.15343", "pmid": "31649194", "doi": "...", "url": "...", "claim": "what the answer uses it for" }
  ]
}
```
`checklist` and `citations` are optional. Put `[1]`, `[2]` markers in `text`; they link to `citations[0]`, `citations[1]`, … in the right-hand Sources panel, which builds arXiv / PubMed / DOI links from the identifiers. Edit `window.sendMessage` in `api.js` if you need streaming or auth.
