# AdiClipAI server

## On a phone (free, no PC): Google Colab
1. Open colab.research.google.com in Chrome, choose Upload and pick `AdiClipAI_Server.ipynb`.
2. Run the cells in order. Upload `viral-clipper.zip` when asked.
3. Open the link printed at the end. That page is the full working AdiClipAI.
Keep the Colab tab open while you use it. Choose Runtime > Change runtime type > T4 GPU for faster transcription.

## On a computer
`pip install -r requirements.txt` (plus ffmpeg and deno), then `python app.py` and open http://localhost:8000

## If YouTube says "Sign in to confirm you're not a bot"
Export your YouTube cookies as `cookies.txt` (Netscape format, with a browser extension) and put it next to app.py.
Optional: GEMINI_API_KEY for better titles, WHISPER_MODEL=small for better Hindi.

## Files
`app.py` (backend), `static/index.html` (AdiClipAI frontend, served at `/`), `requirements.txt`
