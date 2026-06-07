import re
import os
import json
import asyncio
import threading
import subprocess

import anthropic
import httpx
from flask import Flask, jsonify, send_file
from flask_socketio import SocketIO
from flask_cors import CORS
from dotenv import load_dotenv


import os

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY  = os.getenv("DEEPGRAM_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BIBLE_API_KEY     = os.getenv("BIBLE_API_KEY")
BIBLE_API_BASE = "https://api.scripture.api.bible/v1"


# Bible IDs for each version
BIBLE_VERSIONS = {
    "KJV":  "de4e12af7f28f599-02",
    "NIV":  "78a9f6124f344018-01",
    "NKJV": "de4e12af7f28f599-01",
}
DEFAULT_VERSION = "NKJV"

SAMPLE_RATE = 16000
CHANNELS    = 1
BLOCKSIZE   = 4000

CACHE_DIR = os.path.join(os.path.dirname(__file__), "pptx_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── Flask + SocketIO setup ─────────────────────────────────────────────────────
app    = Flask(__name__)
CORS(app)
sio    = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Async loop (runs in background thread) ─────────────────────────────────────
bg_loop           = None
anthropic_client  = None
audio_queue       = None
transcript_queue  = None
live_buffer       = []
heap_buffer       = []


# ── PPTX generation ────────────────────────────────────────────────────────────

def cache_path(passage_id: str, version: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', "_", passage_id)
    return os.path.join(CACHE_DIR, f"{safe}_{version}.pptx")


def generate_pptx(reference: str, verses: list[dict], version: str, passage_id: str) -> str:
    """
    verses = [{"number": 1, "text": "..."}, ...]
    Returns the path to the generated .pptx file.
    """
    out_path   = cache_path(passage_id, version)
    verses_json = json.dumps(verses)
    ref_json    = json.dumps(f"{reference} ({version})")
    path_json   = json.dumps(out_path)

    script = f"""
const pptxgen = require("pptxgenjs");
const verses    = {verses_json};
const reference = {ref_json};

let pres = new pptxgen();
pres.layout = 'LAYOUT_16x9';

verses.forEach((v) => {{
    let slide = pres.addSlide();
    slide.background = {{ color: "000000" }};

    // Reference + verse number — top right
    slide.addText(`${{reference}}  v${{v.number}}`, {{
        x: 0.3, y: 0.2, w: 9.4, h: 0.5,
        fontSize: 18, color: "888888",
        align: "right", fontFace: "Arial"
    }});

    // Verse text — centered
    slide.addText(v.text, {{
        x: 0.5, y: 0.9, w: 9, h: 4.2,
        fontSize: 36, color: "FFFFFF",
        align: "center", valign: "middle",
        fontFace: "Arial", wrap: true
    }});
}});

pres.writeFile({{ fileName: {path_json} }})
    .then(() => process.exit(0))
    .catch(e => {{ console.error(e.message); process.exit(1); }});
"""

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    return out_path


# ── Bible API ──────────────────────────────────────────────────────────────────

async def fetch_chapter(passage_id: str, version: str) -> dict | None:
    """Fetch a chapter from the Bible API and return structured verse list."""
    bible_id = BIBLE_VERSIONS.get(version)
    if not bible_id:
        return None

    url    = f"{BIBLE_API_BASE}/bibles/{bible_id}/passages/{passage_id}"
    params = {
        "content-type":            "text",
        "include-notes":           "false",
        "include-titles":          "false",
        "include-chapter-numbers": "false",
        "include-verse-numbers":   "true",
        "include-verse-spans":     "false",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={"api-key": BIBLE_API_KEY},
                params=params
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})

            # Parse verse content into list of {number, text}
            content   = data.get("content", "")
            reference = data.get("reference", passage_id)
            verses    = parse_verses(content)

            return {
                "reference": reference,
                "version":   version,
                "verses":    verses,
            }
    except httpx.HTTPStatusError as e:
        print(f"[Bible API {e.response.status_code}] {passage_id} {version}")
    except Exception as e:
        print(f"[Bible API error: {e}] {passage_id} {version}")
    return None


def parse_verses(content) -> list[dict]:
    """
    Parse verse-numbered content into [{number, text}, ...].
    Handles plain text with patterns like '[1] In the beginning...'
    or '¹ In the beginning...' depending on the API version.
    """
    # If somehow still a list, flatten to string
    if isinstance(content, list):
        content = " ".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )

    if not isinstance(content, str):
        content = str(content)

    verses  = []

    # Primary pattern: [1] text
    pattern = re.compile(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)', re.DOTALL)
    matches = pattern.findall(content)

    if not matches:
        # Fallback: numbered lines like "1 In the beginning"
        pattern = re.compile(r'(?:^|\n)\s*(\d+)\s+(.*?)(?=\n\s*\d+\s+|$)', re.DOTALL)
        matches = pattern.findall(content)

    for num, text in matches:
        cleaned = re.sub(r'\s+', ' ', text).strip()
        if cleaned:
            verses.append({"number": int(num), "text": cleaned})

    return verses


async def fetch_all_versions(passage_id: str) -> dict:
    """Fetch all 3 versions in parallel. Returns {version: result}."""
    tasks   = {v: fetch_chapter(passage_id, v) for v in BIBLE_VERSIONS}
    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results))


async def resolve_and_cache(passage_id: str):
    """
    Full pipeline for one detected passage:
    1. Check cache for all 3 versions
    2. Fetch missing versions in parallel
    3. Generate PPTXs for fetched versions
    4. Emit result to React frontend via SocketIO
    """
    cached   = {}
    to_fetch = []

    for version in BIBLE_VERSIONS:
        path = cache_path(passage_id, version)
        if os.path.exists(path):
            cached[version] = path
        else:
            to_fetch.append(version)

    # Emit immediately with what's cached so UI can show something fast
    if cached:
        sio.emit("scripture_ready", {
            "passage_id": passage_id,
            "cached":     cached,
            "complete":   len(to_fetch) == 0,
        })

    if not to_fetch:
        print(f"[cache hit] {passage_id} — all versions ready")
        return

    # Fetch missing versions in parallel
    print(f"[fetching] {passage_id} — {to_fetch}")
    fetch_tasks = [fetch_chapter(passage_id, v) for v in to_fetch]
    fetched     = await asyncio.gather(*fetch_tasks)

    new_paths = {}
    for version, result in zip(to_fetch, fetched):
        if not result:
            continue
        try:
            path = await asyncio.get_event_loop().run_in_executor(
                None,
                generate_pptx,
                result["reference"],
                result["verses"],
                version,
                passage_id,
            )
            new_paths[version] = path
            print(f"[✓ PPTX] {passage_id} {version}")
        except Exception as e:
            print(f"[PPTX error] {passage_id} {version}: {e}")

    all_paths = {**cached, **new_paths}
    sio.emit("scripture_ready", {
        "passage_id": passage_id,
        "cached":     all_paths,
        "complete":   True,
    })


# ── Claude scripture detection ─────────────────────────────────────────────────

SCRIPTURE_PROMPT = """
You are analyzing a live church sermon transcript captured in real-time via Deepgram.

Your sole task: extract Bible scripture references from each chunk.

TRANSCRIPT NOISE — handle intelligently:
- "Some one" → Psalm 1
- "Some" → Psalm
- Filler words (er, uh) → ignore
- "six teen" → 16
- We only need BOOK + CHAPTER (not verse) — return whole chapter ID

OUTPUT FORMAT — JSON only, no markdown, no explanation:
{
  "scriptures": ["USFM_CHAPTER_ID", ...]
}

USFM chapter format: GEN.6  ROM.8  JHN.3  PSA.23
Book codes: GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOE AMO OBA JON MIC NAH HAB ZEP HAG ZEC MAL MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV

Rules:
- Return whole chapter only — ignore verse numbers, they're for the operator
- No duplicates
- If nothing found: { "scriptures": [] }

EXAMPLES:
"turn to Genesis chapter 6 verse 22" → { "scriptures": ["GEN.6"] }
"Romans 8 and Galatians 5" → { "scriptures": ["ROM.8", "GAL.5"] }
"the Lord said unto him" → { "scriptures": [] }
"""


def clean_json(raw: str) -> str:
    return re.sub(r"```(?:json)?", "", raw).strip()


async def detect_scripture(text: str):
    try:
        response = await anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            system=SCRIPTURE_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        raw        = clean_json(response.content[0].text)
        result     = json.loads(raw)
        scriptures = result.get("scriptures", [])
        if scriptures:
            print(f"[detected] {scriptures}")
            for pid in scriptures:
                asyncio.create_task(resolve_and_cache(pid))
    except json.JSONDecodeError as e:
        print(f"[parse error: {e}]")
    except Exception as e:
        print(f"[detection error: {e}]")


# ── Transcript processing ──────────────────────────────────────────────────────

async def process_sentences():
    global live_buffer, heap_buffer
    while True:
        sentence = await transcript_queue.get()
        heap_buffer.append(sentence)
        live_buffer.append(sentence)
        if len(live_buffer) > 3:
            live_buffer.pop(0)
        # Emit live transcript to frontend
        sio.emit("transcript", {"text": sentence})
        asyncio.create_task(detect_scripture(" ".join(live_buffer)))


# ── Deepgram audio stream ──────────────────────────────────────────────────────

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    f"?encoding=linear16&sample_rate={SAMPLE_RATE}"
    "&channels=1&language=en&model=nova-2"
    "&smart_format=true&interim_results=true"
    "&utterance_end_ms=1000&vad_events=true"
)


async def deepgram_stream():
    import websockets, inspect, sounddevice as sd

    connect_sig  = inspect.signature(websockets.connect)
    header_kwarg = (
        "additional_headers"
        if "additional_headers" in connect_sig.parameters
        else "extra_headers"
    )

    def audio_callback(indata, frames, time, status):
        pcm = (indata[:, 0] * 32767).astype("int16").tobytes()
        bg_loop.call_soon_threadsafe(audio_queue.put_nowait, pcm)

    async with websockets.connect(
        DEEPGRAM_URL,
        **{header_kwarg: {"Authorization": f"Token {DEEPGRAM_API_KEY}"}}
    ) as ws:
        print("✓ Connected to Deepgram")
        sio.emit("status", {"connected": True})

        async def sender():
            while True:
                chunk = await audio_queue.get()
                if chunk is None:
                    await ws.send(json.dumps({"type": "CloseStream"}))
                    break
                await ws.send(chunk)

        async def receiver():
            async for msg in ws:
                data       = json.loads(msg)
                if data.get("type") != "Results":
                    continue
                transcript = (
                    data.get("channel", {})
                        .get("alternatives", [{}])[0]
                        .get("transcript", "")
                        .strip()
                )
                if not transcript:
                    continue
                if data.get("is_final"):
                    print(f"\r{transcript}")
                    await transcript_queue.put(transcript)
                else:
                    sio.emit("transcript_interim", {"text": transcript})

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", blocksize=BLOCKSIZE,
            callback=audio_callback,
        ):
            print("🎙  Listening...\n")
            await asyncio.gather(sender(), receiver())


async def bg_main():
    global anthropic_client, audio_queue, transcript_queue
    anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    audio_queue      = asyncio.Queue()
    transcript_queue = asyncio.Queue()

    await asyncio.gather(deepgram_stream(), process_sentences())


def start_bg_loop():
    global bg_loop
    bg_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bg_loop)
    bg_loop.run_until_complete(bg_main())


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/pptx/<path:filename>")
def serve_pptx(filename):
    """Serve a cached PPTX file for download/drag."""
    full_path = os.path.join(CACHE_DIR, filename)
    if not os.path.exists(full_path):
        return jsonify({"error": "not found"}), 404
    return send_file(full_path, as_attachment=True)


@app.route("/cache")
def list_cache():
    """List all cached PPTXs."""
    files = os.listdir(CACHE_DIR)
    return jsonify({"files": files})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t = threading.Thread(target=start_bg_loop, daemon=True)
    t.start()

    print("EasyWorship Helper backend running on http://localhost:5000\n")
    sio.run(app, host="0.0.0.0", port=5000, debug=False)