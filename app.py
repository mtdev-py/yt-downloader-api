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
    # Validate it looks like Netscape format
    text = cookies_txt.strip()
    if not (text.startswith("# Netscape") or text.startswith("# HTTP Cookie") or "\t" in text):
        return None  # Invalid format, skip
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
    """Opções base com proxy residencial."""
    PROXY = "http://b2eac90fa0783f06acd6__cr.br:b58b7ea3fafc8b71@67.213.114.47:823"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": cookie_file,
        "proxy": PROXY,
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

@app.route("/test-proxy", methods=["GET"])
def test_proxy():
    """Test if the residential proxy is working from Railway."""
    import requests as req
    PROXY = "http://b2eac90fa0783f06acd6__cr.br:b58b7ea3fafc8b71@67.213.114.47:823"
    results = {}

    # 1. Railway's real IP
    try:
        r = req.get("https://httpbin.org/ip", timeout=10)
        results["railway_ip"] = r.json()
    except Exception as e:
        results["railway_ip_error"] = str(e)

    # 2. IP through proxy
    try:
        r = req.get("https://httpbin.org/ip", proxies={"http": PROXY, "https": PROXY}, timeout=15)
        results["proxy_ip"] = r.json()
        results["proxy_works"] = True
    except Exception as e:
        results["proxy_ip_error"] = str(e)
        results["proxy_works"] = False

    # 3. yt-dlp with proxy — test format extraction
    try:
        opts = {
            "quiet": True, "no_warnings": True,
            "proxy": PROXY,
            "skip_download": True,
            "ignore_no_formats_error": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        fmts = info.get("formats", [])
        audio_fmts = [f for f in fmts if f.get("acodec") != "none" and f.get("vcodec") == "none"]
        results["ytdlp_proxy"] = {
            "total_formats": len(fmts),
            "audio_formats": len(audio_fmts),
            "title": info.get("title"),
        }
    except Exception as e:
        results["ytdlp_proxy_error"] = str(e)

    # 4. yt-dlp WITHOUT proxy for comparison
    try:
        opts2 = {
            "quiet": True, "no_warnings": True,
            "skip_download": True,
            "ignore_no_formats_error": True,
        }
        with yt_dlp.YoutubeDL(opts2) as ydl:
            info2 = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        fmts2 = info2.get("formats", [])
        results["ytdlp_no_proxy"] = {
            "total_formats": len(fmts2),
        }
    except Exception as e:
        results["ytdlp_no_proxy_error"] = str(e)

    return jsonify(results)

@app.route("/convert", methods=["POST"])
def convert():
    """Receive raw audio blob from extension, convert with FFmpeg."""
    audio_file = request.files.get("audio")
    fmt = request.form.get("fmt", "mp3")
    quality = request.form.get("quality", "192")
    title = request.form.get("title", "audio")

    if not audio_file:
        return jsonify({"error": "No audio file provided"}), 400

    tmpdir = tempfile.mkdtemp(prefix="conv_")
    try:
        # Save uploaded audio
        input_ext = audio_file.filename.rsplit(".", 1)[-1] if "." in audio_file.filename else "webm"
        input_path = os.path.join(tmpdir, f"input.{input_ext}")
        audio_file.save(input_path)

        output_path = os.path.join(tmpdir, f"output.{fmt}")

        # Convert with FFmpeg
        import subprocess
        cmd = ["ffmpeg", "-i", input_path, "-y"]
        if fmt == "mp3":
            cmd += ["-codec:a", "libmp3lame", "-b:a", f"{quality}k"]
        elif fmt == "m4a":
            cmd += ["-codec:a", "aac", "-b:a", f"{quality}k"]
        elif fmt == "opus":
            cmd += ["-codec:a", "libopus", "-b:a", f"{quality}k"]
        elif fmt == "flac":
            cmd += ["-codec:a", "flac"]
        elif fmt == "wav":
            cmd += ["-codec:a", "pcm_s16le"]
        else:
            cmd += ["-codec:a", "libmp3lame", "-b:a", f"{quality}k"]
            fmt = "mp3"

        cmd.append(output_path)
        result = subprocess.run(cmd, capture_output=True, timeout=120)

        if not os.path.exists(output_path):
            return jsonify({"error": f"FFmpeg conversion failed: {result.stderr.decode()[-200:]}"}), 500

        final_name = sanitize_filename(title) + f".{fmt}"
        mime = "audio/mp4" if fmt == "m4a" else "audio/mpeg"

        def generate():
            try:
                with open(output_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                cleanup(tmpdir=tmpdir)

        headers = {
            "Content-Disposition": safe_content_disposition(final_name),
            "Content-Length": str(os.path.getsize(output_path)),
        }
        return Response(generate(), headers=headers, mimetype=mime)

    except Exception as e:
        cleanup(tmpdir=tmpdir)
        return jsonify({"error": str(e)}), 500

@app.route("/info", methods=["POST"])
def info():
    data = request.get_json()
    url = data.get("url")
    cookies_txt = data.get("cookies", "")

    if not url:
        return jsonify({"error": "URL not provided"}), 400

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
                PROXY = "http://b2eac90fa0783f06acd6__cr.br:b58b7ea3fafc8b71@67.213.114.47:823"
                opts = {
                    "quiet": True, "no_warnings": True,
                    "cookiefile": cookie_file,
                    "proxy": PROXY,
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