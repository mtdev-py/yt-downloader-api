import os
import shutil
import tempfile
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import yt_dlp
import unicodedata

app = Flask(__name__)
CORS(app)

# ── Helpers ───────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in name if c.isalnum() or c in " .-_()[]{}").strip()

def safe_content_disposition(filename: str) -> str:
    try:
        filename.encode("latin-1")
        return f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        from urllib.parse import quote
        ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii").strip()
        utf8_name = quote(filename, safe="")
        return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'

def write_cookie_file(cookies_txt):
    """Cria cookiefile temporário. Caller é responsável por deletar."""
    if not cookies_txt or not cookies_txt.strip():
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(cookies_txt)
    tmp.close()
    return tmp.name

def cleanup(cookie_file=None, tmpdir=None):
    """Remove arquivos temporários."""
    if cookie_file and os.path.exists(cookie_file):
        os.unlink(cookie_file)
    if tmpdir and os.path.exists(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)

def make_ydl_opts(cookie_file, tmpdir=None):
    """Opções resilientes do yt-dlp conforme spec."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "format": "bestvideo+bestaudio/best",
        "geo_bypass": True,
        "nocheckcertificate": True,
        "cookiefile": cookie_file,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"],
            }
        },
    }
    if tmpdir:
        opts["outtmpl"] = os.path.join(tmpdir, "%(title)s.%(ext)s")
    return opts

# ── Routes ────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/info", methods=["POST"])
def info():
    data = request.get_json()
    url = data.get("url")
    cookies_txt = data.get("cookies", "")

    if not url:
        return jsonify({"error": "URL not provided"}), 400

    if not cookies_txt or not cookies_txt.strip():
        return jsonify({"error": "Cookies required. Please install Chrome extension and login to YouTube."}), 400

    cookie_file = write_cookie_file(cookies_txt)
    try:
        opts = make_ydl_opts(cookie_file)
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(url, download=False)

        duration = result.get("duration", 0)
        minutes, seconds = divmod(int(duration), 60)

        return jsonify({
            "id":        result.get("id"),
            "title":     result.get("title"),
            "thumbnail": result.get("thumbnail"),
            "uploader":  result.get("uploader"),
            "duration":  f"{minutes}:{seconds:02d}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        cleanup(cookie_file=cookie_file)

@app.route("/download", methods=["POST"])
def download():
    url         = request.form.get("url")
    mode_       = request.form.get("mode", "audio")
    quality     = request.form.get("quality", "192")
    fmt         = request.form.get("fmt", "mp3")
    video_fmt   = request.form.get("video_fmt", "mp4")
    cookies_txt = request.form.get("cookies", "")

    if not url:
        return jsonify({"error": "URL not provided"}), 400

    if not cookies_txt or not cookies_txt.strip():
        return jsonify({"error": "Cookies required. Please install Chrome extension and login to YouTube."}), 400

    cookie_file = write_cookie_file(cookies_txt)
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        opts = make_ydl_opts(cookie_file, tmpdir)

        if mode_ == "audio":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": quality},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]
            mime = "audio/mp4" if fmt == "m4a" else "audio/mpeg"
            ext = fmt
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = video_fmt
            mime = "video/mp4"
            ext = video_fmt

        # Step 1: extract_info (download=False)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)

        # Step 2: download
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        # Step 3: find file
        with yt_dlp.YoutubeDL(opts) as ydl:
            filename = ydl.prepare_filename(info_dict)

        if not os.path.exists(filename):
            base = os.path.splitext(filename)[0]
            for e in [ext, "mp3", "m4a", "mp4", "webm", "opus", "ogg"]:
                alt = f"{base}.{e}"
                if os.path.exists(alt):
                    filename = alt
                    break

        if not os.path.exists(filename):
            files = [f for f in os.listdir(tmpdir) if not f.endswith(".part")]
            if files:
                filename = os.path.join(tmpdir, sorted(files)[-1])

        if not os.path.exists(filename):
            return jsonify({"error": "File not found after download"}), 500

        title = info_dict.get("title", "audio")
        final_name = sanitize_filename(title) + f".{ext}"
        if not final_name.strip(f".{ext}"):
            final_name = f"download.{ext}"

        def generate():
            with open(filename, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk
            cleanup(cookie_file=cookie_file, tmpdir=tmpdir)

        headers = {
            "Content-Disposition": safe_content_disposition(final_name),
            "Content-Length": str(os.path.getsize(filename)),
        }
        return Response(generate(), headers=headers, mimetype=mime)

    except Exception as e:
        cleanup(cookie_file=cookie_file, tmpdir=tmpdir)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)