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

def sanitize_filename(name):
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in name if c.isalnum() or c in " .-_()[]{}").strip() or "download"

def safe_content_disposition(filename):
    from urllib.parse import quote
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii").strip() or "download"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename, safe="")}'

def write_cookie_file(cookies_txt):
    if not cookies_txt or not cookies_txt.strip():
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write(cookies_txt)
    tmp.close()
    return tmp.name

def cleanup(cookie_file=None, tmpdir=None):
    if cookie_file and os.path.exists(cookie_file):
        os.unlink(cookie_file)
    if tmpdir and os.path.exists(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)

def base_ydl_opts(cookie_file, tmpdir=None):
    """Opções base — modelado do código local que funciona."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": cookie_file,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "player_skip": ["webpage", "configs"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        "retries": 10,
        "fragment_retries": 10,
        "geo_bypass": True,
        "nocheckcertificate": True,
    }
    if tmpdir:
        opts["outtmpl"] = os.path.join(tmpdir, "%(title)s.%(ext)s")
    return opts

def try_extract(url, opts):
    """Extrai info (e baixa se outtmpl estiver definido).
    Tenta sem player_skip primeiro, depois com diferentes player_clients."""
    # Attempt 1: configuração padrão
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download="outtmpl" in opts and not opts.get("skip_download", False))
    except Exception as e:
        first_error = e

    # Attempt 2: tentar diferentes player_clients
    for clients in [["ios"], ["tv_embedded"], ["web"]]:
        try:
            opts_copy = dict(opts)
            opts_copy["extractor_args"] = {"youtube": {"player_client": clients}}
            with yt_dlp.YoutubeDL(opts_copy) as ydl:
                return ydl.extract_info(url, download="outtmpl" in opts_copy and not opts_copy.get("skip_download", False))
        except Exception:
            continue

    raise first_error

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
        return jsonify({"error": "Cookies required. Login to YouTube first."}), 400

    cookie_file = write_cookie_file(cookies_txt)
    try:
        opts = base_ydl_opts(cookie_file)
        opts["skip_download"] = True

        result = try_extract(url, opts)

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
        return jsonify({"error": "Cookies required."}), 400

    cookie_file = write_cookie_file(cookies_txt)
    tmpdir = tempfile.mkdtemp(prefix="dl_")

    try:
        opts = base_ydl_opts(cookie_file, tmpdir)

        if mode_ == "audio":
            # Fallback chain exata do código que funciona
            opts["format"] = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"
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

        # Single extract_info call — extrai e baixa de uma vez
        info_dict = try_extract(url, opts)

        # Encontrar arquivo (mesma lógica do código que funciona)
        with yt_dlp.YoutubeDL(opts) as ydl:
            filename = ydl.prepare_filename(info_dict)

        if not os.path.exists(filename):
            base = os.path.splitext(filename)[0]
            for e in [ext, "mp3", "m4a", "mp4", "webm", "opus"]:
                alt = f"{base}.{e}"
                if os.path.exists(alt):
                    filename = alt
                    break

        if not os.path.exists(filename):
            files = [f for f in os.listdir(tmpdir) if not f.endswith(".part")]
            if files:
                filename = os.path.join(tmpdir, sorted(files, key=lambda x: os.path.getsize(os.path.join(tmpdir, x)), reverse=True)[0])

        if not os.path.exists(filename):
            return jsonify({"error": "File not found after processing"}), 500

        title = info_dict.get("title", "download")
        final_name = sanitize_filename(title) + f".{ext}"
        if not final_name.strip(f".{ext}"):
            final_name = f"download.{ext}"

        def generate():
            try:
                with open(filename, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                cleanup(cookie_file=cookie_file, tmpdir=tmpdir)

        headers = {
            "Content-Disposition": safe_content_disposition(final_name),
            "Content-Length": str(os.path.getsize(filename)),
        }
        return Response(generate(), headers=headers, mimetype=mime)

    except Exception as e:
        cleanup(cookie_file=cookie_file, tmpdir=tmpdir)
        return jsonify({"error": str(e)}), 500

@app.route("/debug", methods=["POST"])
def debug():
    data = request.get_json()
    url = data.get("url")
    cookies_txt = data.get("cookies", "")

    if not url:
        return jsonify({"error": "URL not provided"}), 400

    cookie_file = write_cookie_file(cookies_txt)
    try:
        clients_to_test = ["android", "web", "ios", "tv_embedded"]
        results = {}

        for client in clients_to_test:
            try:
                opts = {
                    "quiet": True, "no_warnings": True,
                    "cookiefile": cookie_file,
                    "skip_download": True,
                    "geo_bypass": True, "nocheckcertificate": True,
                    "ignore_no_formats_error": True,
                    "extractor_args": {"youtube": {"player_client": [client]}},
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                fmts = info.get("formats", [])
                downloadable = [f for f in fmts if f.get("url")]
                results[client] = {
                    "total": len(fmts),
                    "downloadable": len(downloadable),
                    "formats": [{"id": f.get("format_id"), "ext": f.get("ext"), "res": f.get("resolution"), "acodec": f.get("acodec"), "vcodec": f.get("vcodec")} for f in fmts[:10]],
                }
            except Exception as e:
                results[client] = {"error": str(e)[:100]}

        title = "unknown"
        for client in clients_to_test:
            r = results.get(client, {})
            if "error" not in r and r.get("total", 0) > 0:
                try:
                    opts = {"quiet": True, "cookiefile": cookie_file, "skip_download": True, "ignore_no_formats_error": True, "extractor_args": {"youtube": {"player_client": [client]}}}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    title = info.get("title", "unknown")
                    break
                except: pass

        return jsonify({
            "yt_dlp_version": yt_dlp.version.__version__,
            "title": title,
            "clients": results,
            "has_cookies": bool(cookies_txt.strip()),
        })
    except Exception as e:
        return jsonify({"error": str(e), "yt_dlp_version": yt_dlp.version.__version__}), 400
    finally:
        cleanup(cookie_file=cookie_file)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)