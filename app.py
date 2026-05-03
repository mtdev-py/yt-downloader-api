import os
import shutil
import tempfile
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import yt_dlp
import unicodedata

app = Flask(__name__)
CORS(app)

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
        return jsonify({"error": "Cookies required. Install the Chrome extension and login to YouTube."}), 400

    cookie_file = write_cookie_file(cookies_txt)
    try:
        # Info only — NO format, NO download validation
        opts = {
            "quiet": True,
            "no_warnings": True,
            "cookiefile": cookie_file,
            "skip_download": True,
            "geo_bypass": True,
            "nocheckcertificate": True,
            "extract_flat": False,
            "ignore_no_formats_error": True,
        }

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
        return jsonify({"error": "Cookies required."}), 400

    cookie_file = write_cookie_file(cookies_txt)
    tmpdir = tempfile.mkdtemp(prefix="dl_")

    try:
        # Player clients to try — different clients bypass different restrictions
        client_attempts = [
            ["web"],
            ["ios"],
            ["android"],
            ["tv_embedded"],
            ["web", "ios", "android"],
        ]

        last_error = None

        for clients in client_attempts:
            # Clean tmpdir between attempts
            for f in os.listdir(tmpdir):
                try: os.unlink(os.path.join(tmpdir, f))
                except: pass

            opts = {
                "quiet": True,
                "no_warnings": True,
                "cookiefile": cookie_file,
                "geo_bypass": True,
                "nocheckcertificate": True,
                "retries": 3,
                "fragment_retries": 3,
                "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
                "hls_prefer_native": True,
                "extractor_args": {"youtube": {"player_client": clients}},
            }

            if mode_ == "audio":
                opts["format"] = "best"
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": quality},
                    {"key": "FFmpegMetadata", "add_metadata": True},
                ]
                mime = "audio/mp4" if fmt == "m4a" else "audio/mpeg"
                ext = fmt
            else:
                opts["format"] = "best"
                opts["merge_output_format"] = video_fmt
                mime = "video/mp4"
                ext = video_fmt

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info_dict = ydl.extract_info(url, download=True)
                break  # success!
            except Exception as e:
                last_error = e
                continue

        else:
            # All attempts failed
            raise last_error or Exception("All player client attempts failed")

        # Find file
        filename = None
        files = [f for f in os.listdir(tmpdir) if not f.endswith((".part", ".temp", ".ytdl"))]
        if files:
            filename = os.path.join(tmpdir, max(files, key=lambda f: os.path.getsize(os.path.join(tmpdir, f))))

        if not filename or not os.path.exists(filename):
            return jsonify({"error": "File not found after processing"}), 500

        title = info_dict.get("title", "download")
        final_name = sanitize_filename(title) + f".{ext}"

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
        clients_to_test = ["web", "ios", "android", "tv_embedded"]
        results = {}

        for client in clients_to_test:
            try:
                opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "cookiefile": cookie_file,
                    "skip_download": True,
                    "geo_bypass": True,
                    "nocheckcertificate": True,
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
                    "formats": [{"id": f.get("format_id"), "ext": f.get("ext"), "res": f.get("resolution"), "acodec": f.get("acodec"), "vcodec": f.get("vcodec"), "url_ok": bool(f.get("url"))} for f in fmts[:10]],
                }
            except Exception as e:
                results[client] = {"error": str(e)}

        # Also get title from first working client
        title = "unknown"
        for client in clients_to_test:
            if "error" not in results.get(client, {}):
                try:
                    opts = {
                        "quiet": True, "cookiefile": cookie_file,
                        "skip_download": True, "ignore_no_formats_error": True,
                        "extractor_args": {"youtube": {"player_client": [client]}},
                    }
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    title = info.get("title", "unknown")
                    break
                except:
                    pass

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