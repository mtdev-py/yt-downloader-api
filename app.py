import os
import shutil
import tempfile
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import yt_dlp
import unicodedata

app = Flask(__name__)
CORS(app)  # Permite requisições da extensão Chrome (origem diferente)

def sanitize_filename(name: str) -> str:
    # Normaliza unicode (ex: é -> e) e remove caracteres inválidos
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in name if c.isalnum() or c in " .-_()[]{}").strip()

def safe_content_disposition(filename: str) -> str:
    """Gera header Content-Disposition compatível com latin-1."""
    try:
        filename.encode("latin-1")
        return f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        # RFC 5987: usa UTF-8 encoded filename
        from urllib.parse import quote
        ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii").strip()
        utf8_name  = quote(filename, safe="")
        return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{utf8_name}'

# Clientes a tentar em sequência
YT_CLIENTS = [
    ["ios"],
    ["android_embedded"],
    ["web_creator"],
    ["tv_embedded"],
    ["android", "web"],
]

def write_cookie_file(cookies_txt):
    """Escreve cookies recebidos da extensão em um arquivo temporário."""
    if not cookies_txt or not cookies_txt.strip():
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                      delete=False, encoding="utf-8")
    tmp.write(cookies_txt)
    tmp.close()
    return tmp.name

def base_ydl_opts(tmpdir=None, cookie_file=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios"],
                "player_skip": ["webpage", "configs"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Mobile Safari/537.36"
            ),
        },
        "retries": 5,
        "fragment_retries": 5,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if tmpdir:
        opts["outtmpl"] = os.path.join(tmpdir, "%(title)s.%(ext)s")
    return opts

def try_extract(url, opts):
    """Tenta múltiplos player_clients do YouTube até um funcionar."""
    last_error = None

    for clients in YT_CLIENTS:
        try:
            opts_copy = dict(opts)
            opts_copy["extractor_args"] = {
                "youtube": {
                    "player_client": clients,
                    "player_skip": ["webpage", "configs"],
                }
            }
            with yt_dlp.YoutubeDL(opts_copy) as ydl:
                is_download = "outtmpl" in opts_copy and not opts_copy.get("skip_download", False)
                return ydl.extract_info(url, download=is_download)
        except Exception as e:
            last_error = e
            continue

    raise Exception(
        f"Could not process this video. "
        f"It may be restricted or temporarily unavailable. "
        f"Detail: {last_error}"
    )

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

    cookie_file = write_cookie_file(cookies_txt)
    try:
        opts = base_ydl_opts(cookie_file=cookie_file)
        opts["skip_download"] = True

        info = try_extract(url, opts)

        duration = info.get("duration", 0)
        minutes, seconds = divmod(int(duration), 60)

        return jsonify({
            "id":        info.get("id"),
            "title":     info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "uploader":  info.get("uploader"),
            "duration":  f"{minutes}:{seconds:02d}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        if cookie_file and os.path.exists(cookie_file):
            os.unlink(cookie_file)

@app.route("/download", methods=["POST"])
def download():
    url         = request.form.get("url")
    mode        = request.form.get("mode", "audio")
    quality     = request.form.get("quality", "192")
    fmt         = request.form.get("fmt", "mp3")
    video_fmt   = request.form.get("video_fmt", "mp4")
    cookies_txt = request.form.get("cookies", "")

    if not url:
        return "URL not provided", 400

    cookie_file = write_cookie_file(cookies_txt)
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        opts = base_ydl_opts(tmpdir, cookie_file=cookie_file)

        if mode == "audio":
            opts["format"] = "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best"
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": quality},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]
            mime = "audio/mp4" if fmt == "m4a" else "audio/mpeg"
            ext  = fmt
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = video_fmt
            mime = "video/mp4"
            ext  = video_fmt

        info = try_extract(url, opts)

        with yt_dlp.YoutubeDL(opts) as ydl:
            filename = ydl.prepare_filename(info)

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
                filename = os.path.join(tmpdir, sorted(files)[-1])

        title      = info.get("title", "audio")
        final_name = sanitize_filename(title) + f".{ext}"
        if not final_name.strip(f".{ext}"):
            final_name = f"audio.{ext}"

        def generate():
            with open(filename, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk
            try:
                shutil.rmtree(tmpdir)
            except Exception:
                pass
            if cookie_file and os.path.exists(cookie_file):
                os.unlink(cookie_file)

        headers = {
            "Content-Disposition": safe_content_disposition(final_name),
            "Content-Length":      str(os.path.getsize(filename)),
        }
        return Response(generate(), headers=headers, mimetype=mime)

    except Exception as e:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass
        if cookie_file and os.path.exists(cookie_file):
            os.unlink(cookie_file)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_local = port == 5000
    if is_local:
        print("\n🎵 YouTube Downloader rodando em: http://127.0.0.1:5000\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)