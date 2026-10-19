"""`quern record download` (#482): a recording's files, copied from the
server -- a remote one under QUERN_REMOTE -- for a CI job, as the MCP's
download_recording does for an agent.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from server.recording import cli

REC = "rec_0123456789ab"
FILES = {"events.jsonl": b'{"type": "started"}\n', "manifest.json": b"{}",
         "video-1.mp4": b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000}


class Server:
    def __init__(self, files=FILES, listing_extra=(), fail=(), status=200, live=False):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, kind="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                base = f"/api/v1/recordings/{REC}/files"
                if status != 200:
                    self._send(status, json.dumps({"detail": f"no recording {REC!r}"}).encode())
                elif self.path == base:
                    listing = [{"name": n, "bytes": len(b)} for n, b in files.items()]
                    listing += [{"name": n, "bytes": 1} for n in listing_extra]
                    self._send(200, json.dumps({"recording_id": REC, "live": live,
                                                "files": listing}).encode())
                elif self.path.startswith(base + "/"):
                    name = self.path.rsplit("/", 1)[1]
                    if name in fail:
                        self._send(500, b'{"detail": "boom"}')
                    else:
                        self._send(200, files[name], "application/octet-stream")
                else:
                    self._send(404, b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def serve(monkeypatch):
    made = []

    def start(**kw):
        s = Server(**kw)
        made.append(s)
        monkeypatch.setattr(cli, "_target", lambda: (s.url, "qc_key", None))
        return s

    yield start
    for s in made:
        s.close()


def test_every_file_lands_whole_and_nothing_half(serve, tmp_path, capsys):
    s = serve()
    dest = tmp_path / "out"
    assert cli.main(["download", REC, "--dest", str(dest)]) == 0
    for name, body in FILES.items():
        assert (dest / name).read_bytes() == body
    assert not list(dest.glob("*.part"))
    assert capsys.readouterr().out.strip() == str(dest)
    assert all(auth == "Bearer qc_key" for _, auth in s.seen)


def test_the_default_destination_is_under_the_current_directory(serve, tmp_path, monkeypatch):
    serve()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["download", REC]) == 0
    assert (tmp_path / "quern-recordings" / REC / "events.jsonl").is_file()


def test_only_a_recording_id_is_taken(serve, tmp_path, monkeypatch, capsys):
    """The default destination is built from it: '../x' would have been a
    directory outside."""
    s = serve()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["download", "../escape"]) == 2
    assert "not a recording id" in capsys.readouterr().err
    assert s.seen == [] and not (tmp_path.parent / "escape").exists()


def test_a_name_no_recording_writes_is_not_written(serve, tmp_path):
    serve(listing_extra=["../../evil", "notes.txt"])
    dest = tmp_path / "out"
    assert cli.main(["download", REC, "--dest", str(dest)]) == 0
    assert sorted(p.name for p in dest.iterdir()) == sorted(FILES)
    assert not (tmp_path / "evil").exists()


def test_another_keys_recording_is_refused_with_the_servers_words(serve, tmp_path, capsys):
    serve(status=404)
    assert cli.main(["download", REC, "--dest", str(tmp_path / "out")]) == 2
    assert "no recording" in capsys.readouterr().err


def test_a_file_that_fails_leaves_no_part_and_says_which(serve, tmp_path, capsys):
    serve(fail={"video-1.mp4"})
    dest = tmp_path / "out"
    assert cli.main(["download", REC, "--dest", str(dest)]) == 2
    assert "video-1.mp4" in capsys.readouterr().err
    assert not (dest / "video-1.mp4").exists() and not list(dest.glob("*.part"))


def test_a_live_recording_says_so(serve, tmp_path, capsys):
    serve(live=True)
    assert cli.main(["download", REC, "--dest", str(tmp_path / "out")]) == 0
    assert "still recording" in capsys.readouterr().err


def test_no_server_is_exit_1(monkeypatch, capsys):
    def nowhere():
        raise ConnectionError("no server answering: start it with `quern start`")

    monkeypatch.setattr(cli, "_target", nowhere)
    assert cli.main(["download", REC]) == 1
    assert "no server answering" in capsys.readouterr().err


def test_start_and_stop_say_a_remote_recordings_paths_are_on_the_server(monkeypatch, capsys):
    monkeypatch.setenv("QUERN_REMOTE", "office")
    cli._remote_note(REC)
    assert f"quern record download {REC}" in capsys.readouterr().err
    monkeypatch.delenv("QUERN_REMOTE")
    cli._remote_note(REC)
    assert capsys.readouterr().err == ""
