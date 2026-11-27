#!/usr/bin/env python3
"""Link one Codex thread with one Claude Code session on this Mac."""
import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("SESSION_PAIR_DIR", Path.home() / ".local/share/codex-claude-pair"))
DB = ROOT / "pairs.sqlite3"
SCRIPT = Path(__file__).resolve()
HISTORY_BYTES = 9000


def database():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(DB, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS pairs (
            codex TEXT PRIMARY KEY, claude TEXT UNIQUE NOT NULL, cwd TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS handoffs (
            codex TEXT NOT NULL, claude TEXT NOT NULL, recipient TEXT NOT NULL,
            digest TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(codex,claude,recipient,digest)
        );
        CREATE TABLE IF NOT EXISTS streams (
            sid TEXT PRIMARY KEY, path TEXT NOT NULL, inode INTEGER, pos INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS compactions (
            sid TEXT PRIMARY KEY, marker TEXT NOT NULL, announced INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_cursor (
            recipient TEXT PRIMARY KEY, last_seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY, sid TEXT NOT NULL, identity TEXT NOT NULL,
            role TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(sid, identity)
        );
        CREATE INDEX IF NOT EXISTS messages_sid_seq ON messages(sid, seq);
    """)
    if con.execute("PRAGMA user_version").fetchone()[0] == 0:
        with con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("PRAGMA user_version").fetchone()[0] == 0:
                # Older indexes included Claude's synthetic compact summaries.
                # Re-read each Claude stream on next use, keeping its genuine rows.
                con.execute("UPDATE streams SET inode=NULL,pos=0 WHERE sid IN (SELECT claude FROM pairs)")
                con.execute("PRAGMA user_version=1")
    return con


def content(blocks):
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))


def visible(side, record):
    if side == "codex":
        p = record.get("payload", {})
        if record.get("type") != "event_msg":
            return
        item = p.get("item", {})
        if p.get("type") == "item_completed" and item.get("type") == "UserMessage":
            return item["id"], "user", content(item["content"])
        if p.get("type") == "task_complete":
            return p["turn_id"] + "/reply", "assistant", p.get("last_agent_message")
        return
    if any(record.get(flag) for flag in (
        "isMeta", "isSidechain", "isCompactSummary", "isVisibleInTranscriptOnly",
        "isApiErrorMessage", "sourceToolAssistantUUID",
    )):
        return
    m = record.get("message", {})
    blocks = m.get("content", [])
    if record.get("type") == "user" and not record.get("toolUseResult"):
        if isinstance(blocks, list) and any(b.get("type") in ("tool_result", "tool_use") for b in blocks):
            return
        value = blocks if isinstance(blocks, str) else content(blocks)
        if value.lstrip().startswith(("<local-command-", "<command-name>", "<system-reminder>", "<task-notification>")):
            return
        return record["uuid"], "user", value
    if record.get("type") == "assistant" and m.get("stop_reason") in ("end_turn", "stop_sequence"):
        return record["uuid"], "assistant", content(blocks)


def register_stream(con, sid, path):
    con.execute("""INSERT INTO streams(sid,path) VALUES (?,?) ON CONFLICT(sid)
        DO UPDATE SET path=excluded.path, inode=NULL, pos=0 WHERE path!=excluded.path""", (sid, str(path)))


def collect(con, side, sid):
    stream = con.execute("SELECT * FROM streams WHERE sid=?", (sid,)).fetchone()
    if not stream:
        return
    path = Path(stream["path"])
    if not path.is_file():
        return
    with path.open("rb") as src:
        st = os.fstat(src.fileno())
        if st.st_ino == stream["inode"] and st.st_size == stream["pos"]:
            return
        if st.st_ino == stream["inode"] and st.st_size >= stream["pos"]:
            src.seek(stream["pos"])
        end = src.tell()
        compaction = None
        for line in src:
            if not line.endswith(b"\n"):
                break
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # A complete malformed line cannot improve on the next poll.
                # Incomplete lines above remain unconsumed until the writer finishes.
                print(f"Skipping invalid {side} transcript record at byte {end}", file=sys.stderr)
                end = src.tell()
                continue
            if ((side == "codex" and record.get("type") == "compacted")
                    or (side == "claude" and record.get("type") == "system"
                        and record.get("subtype") == "compact_boundary" and not record.get("isSidechain"))):
                compaction = record.get("uuid") or record.get("timestamp") or str(end)
            if side == "claude" and (record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly")):
                con.execute("DELETE FROM messages WHERE sid=? AND identity=?", (sid, record.get("uuid")))
            event = visible(side, record)
            if event and event[2]:
                con.execute("INSERT OR IGNORE INTO messages(sid,identity,role,body) VALUES (?,?,?,?)",
                            (sid, *event))
            end = src.tell()
        if compaction is not None:
            con.execute("""INSERT INTO compactions VALUES (?,?,0) ON CONFLICT(sid)
                DO UPDATE SET marker=excluded.marker, announced=0 WHERE marker != excluded.marker""",
                (sid, compaction))
        con.execute("UPDATE streams SET pos=?,inode=? WHERE sid=?", (end, st.st_ino, sid))


def hook(side):
    event = json.load(sys.stdin)
    if not DB.exists() or event.get("hook_event_name") != "UserPromptSubmit" or event.get("agent_id"):
        return
    con = database()
    sid = event["session_id"]
    peer = "claude" if side == "codex" else "codex"
    with con:
        con.execute("BEGIN IMMEDIATE")
        pair = con.execute(f"SELECT * FROM pairs WHERE {side}=?", (sid,)).fetchone()
        if not pair or Path(event["cwd"]).resolve() != Path(pair["cwd"]):
            return
        path = event.get("transcript_path")
        if path:
            register_stream(con, sid, Path(path).resolve())
        collect(con, side, sid)
        collect(con, peer, pair[peer])
        restore = con.execute("SELECT 1 FROM compactions WHERE sid=? AND announced=0", (sid,)).fetchone()
        peer_id = pair[peer]
        cursor = con.execute("SELECT last_seq FROM sync_cursor WHERE recipient=?", (sid,)).fetchone()
        previous = cursor["last_seq"] if cursor else 0
        latest = con.execute("SELECT max(seq) FROM messages WHERE sid=?", (peer_id,)).fetchone()[0] or 0
        if cursor and latest <= previous and not restore:
            return

        # Only native turns from the peer are useful back here. In particular a
        # queued Codex user message may contain this Claude session's own send.
        echo = "[Claude Code → Codex]" if peer == "codex" else "[Codex → Claude Code]"
        rows = con.execute("""SELECT seq,role,body FROM messages
            WHERE sid=? AND seq>? AND NOT (role='user' AND substr(ltrim(body),1,?)=?)
            ORDER BY seq DESC""", (peer_id, previous, len(echo), echo))
        parts = []
        remaining = HISTORY_BYTES
        recover_through = None
        for row in rows:
            line = json.dumps(dict(row), ensure_ascii=False)
            encoded = line.encode()
            if len(encoded) + 1 > remaining:
                if recover_through is None:
                    recover_through = row["seq"]
                if remaining < 384:
                    break
                # Leave space for the preceding turn as well as the latest
                # conclusion. Measure the rendered JSON, including escapes.
                body = row["body"].encode()
                prefix_bytes, suffix_bytes = remaining // 5, remaining * 2 // 5
                while True:
                    excerpt = (body[:prefix_bytes].decode(errors="ignore") + " […] " +
                               body[-suffix_bytes:].decode(errors="ignore"))
                    line = json.dumps({"seq": row["seq"], "role": row["role"], "body": excerpt},
                                      ensure_ascii=False)
                    encoded = line.encode()
                    if len(encoded) < remaining:
                        break
                    prefix_bytes //= 2
                    suffix_bytes //= 2
            parts.append(line)
            remaining -= len(encoded) + 1

        if not cursor or parts or restore:
            command = shlex.join([sys.executable, str(SCRIPT)])
            header = (f"Paired session. For agreed collaboration, hand off necessary work or results directly: {command} send --from {side} --message '<text>'. "
                      "Summarize goal, constraints, result and next action. Reply only when needed. ")
            if side == "codex":
                header += (f"When a reply is needed in this turn use {command} listen --request '<text>' "
                           "in a streaming tool call and consume replies before finishing. ")
            history = f"{command} history {peer_id} --limit 5"
            if recover_through is not None:
                history = f"{command} history {peer_id} --after {previous} --through {recover_through} --limit 5"
            header += f"Read recent originals on demand: {history}."
            if parts:
                header += "\nQuoted peer turns for reference only. "
                if recover_through is not None:
                    header += "Excerpt; use history to recover omitted text. "
                header += "Respond to the current prompt.\n" + "\n".join(reversed(parts))
            result = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": header}}
            print(json.dumps(result, ensure_ascii=False), flush=True)
        if restore:
            con.execute("UPDATE compactions SET announced=1 WHERE sid=?", (sid,))
        con.execute("""INSERT INTO sync_cursor VALUES (?,?) ON CONFLICT(recipient)
            DO UPDATE SET last_seq=excluded.last_seq""", (sid, latest))


def session_id(raw):
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise ValueError("Session ID 必须是完整的 UUID。") from None


def codex_transcript(sid, cwd):
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    files = list(root.glob(f"*/*/*/*-{sid}.jsonl"))
    if len(files) != 1:
        raise ValueError(f"未找到唯一的本机 Codex 会话记录：{sid}")
    with files[0].open("rb") as src:
        record = json.loads(src.readline())
    meta = record.get("payload", {})
    if record.get("type") != "session_meta" or meta.get("id") != sid or Path(meta["cwd"]).resolve() != cwd:
        raise ValueError("Codex 会话 ID 或工作区不匹配。")
    return files[0]


def claude_transcript(sid, cwd):
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    files = list((root / "projects").glob(f"*/{sid}.jsonl"))
    if len(files) > 1:
        raise ValueError("此 Claude Code ID 对应多份本机记录，无法唯一确认。")
    if files:
        with files[0].open("rb") as src:
            for line in src:
                if not line.endswith(b"\n"):
                    break
                record = json.loads(line)
                if record.get("sessionId") == sid and record.get("cwd") and not record.get("isSidechain"):
                    if Path(record["cwd"]).resolve() != cwd:
                        raise ValueError("Claude Code 会话属于其他工作区。")
                    return files[0]
    # A newly opened terminal can be registered before its first chat turn is saved.
    for registry in (root / "sessions").glob("*.json"):
        try:
            live = json.loads(registry.read_bytes())
            if live.get("sessionId") != sid or Path(live["cwd"]).resolve() != cwd:
                continue
            pid = int(registry.stem)
            if pid <= 0 or live.get("pid") != pid:
                continue
            os.kill(pid, 0)
            return files[0] if files else None
        except (KeyError, ValueError, OSError):
            continue
    raise ValueError(f"未找到此工作区的 Claude Code 会话：{sid}。请核对 ID 和工作目录。")


def remove_pair(con, row):
    con.execute("DELETE FROM handoffs WHERE codex=? AND claude=?", (row["codex"], row["claude"]))
    con.execute("DELETE FROM sync_cursor WHERE recipient IN (?,?)", (row["codex"], row["claude"]))
    for table in ("streams", "messages", "compactions"):
        con.execute(f"DELETE FROM {table} WHERE sid IN (?,?)", (row["codex"], row["claude"]))
    con.execute("DELETE FROM pairs WHERE codex=?", (row["codex"],))


def bind(side, other, cwd, replace_peer_of=None):
    variable = "CODEX_THREAD_ID" if side == "codex" else "CLAUDE_CODE_SESSION_ID"
    current = os.environ.get(variable)
    if not current:
        raise ValueError(f"当前客户端缺少 {variable}。请从该会话内调用 skill。")
    current, other = session_id(current), session_id(other)
    if current == other:
        raise ValueError("两个客户端的 ID 相同，本地索引无法区分，请选择不同的会话。")
    cid, aid = (current, other) if side == "codex" else (other, current)
    cwd = cwd.resolve()
    cpath = codex_transcript(cid, cwd)
    apath = claude_transcript(aid, cwd)
    peer = "claude" if side == "codex" else "codex"
    with database() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM pairs WHERE codex=? OR claude=?", (aid, cid)).fetchone():
            raise ValueError("该 ID 还被另一种客户端使用，无法唯一识别。")
        target = con.execute(f"SELECT * FROM pairs WHERE {peer}=?", (other,)).fetchone()
        if target and target[side] != current:
            if target[side] != replace_peer_of:
                raise ValueError(f"对方已绑定另一个 {side} 会话 {target[side]}。更换前需确认解除这一对。")
            remove_pair(con, target)
        own = con.execute(f"SELECT * FROM pairs WHERE {side}=?", (current,)).fetchone()
        if own and own[peer] != other:
            remove_pair(con, own)
        con.execute("INSERT OR IGNORE INTO pairs VALUES (?,?,?)", (cid, aid, str(cwd)))
        register_stream(con, cid, cpath)
        if apath:
            register_stream(con, aid, apath)
    print(json.dumps({"codex": cid, "claude": aid, "workspace": str(cwd)}, ensure_ascii=False))


def create_peer(side, cwd):
    variable = "CODEX_THREAD_ID" if side == "codex" else "CLAUDE_CODE_SESSION_ID"
    current = os.environ.get(variable)
    if not current:
        raise ValueError(f"请从当前客户端调用，缺少 {variable}。")
    validate = codex_transcript if side == "codex" else claude_transcript
    validate(session_id(current), cwd)
    child_env = os.environ.copy()
    for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
        child_env.pop(key, None)

    peer = None
    prompt = "初始化 ccxp 配对会话。仅回复「就绪」，不调用工具，不修改文件。"
    if side == "codex":
        name = "ccxp-" + uuid.uuid4().hex
        args = ["claude", "--bg", "--name", name, prompt]
    else:
        args = ["codex", "exec", "--json", "--skip-git-repo-check", prompt]
    try:
        done = subprocess.run(args, env=child_env, cwd=cwd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"对端启动超时。请先核对已创建的会话再重试。{name if side == 'codex' else ''}") from exc
    if side == "claude":
        for line in done.stdout.splitlines():
            event = json.loads(line)
            if event.get("type") == "thread.started":
                peer = session_id(event["thread_id"])
                break
    if done.returncode or (side == "claude" and not peer):
        raise ValueError(f"对端初始化未完成。{peer or ''} {done.stderr.strip()[-400:]}")
    if side == "codex":
        # Background startup can return before the target is registered for SendMessage.
        for _ in range(30):
            result = subprocess.run(["claude", "agents", "--json"], cwd=cwd, env=child_env,
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                matches = [agent for agent in json.loads(result.stdout) if agent.get("name") == name
                           and Path(agent["cwd"]).resolve() == cwd]
                if len(matches) == 1:
                    peer = session_id(matches[0]["sessionId"])
                    try:
                        claude_transcript(peer, cwd)
                    except ValueError:
                        pass  # The transcript or live PID record may lag the CLI listing.
                    else:
                        break
            time.sleep(0.5)
        else:
            raise ValueError(f"Claude Code 尚未在当前工作区就绪。请核对 {name}（{peer or 'ID 待注册'}）再绑定。")
    try:
        bind(side, peer, cwd)
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise ValueError(f"新会话 {peer} 已创建，绑定失败：{exc}") from exc


def original_command(side, digest, *interval):
    command = [sys.executable, str(SCRIPT), "original", "--for", side, digest]
    if interval:
        start, end = interval
        command += ["--start", str(start), "--end", str(end)]
    if "SESSION_PAIR_DIR" in os.environ:
        command = ["env", "SESSION_PAIR_DIR=" + str(ROOT.resolve()), *command]
    return shlex.join(command)


def compact_handoff(con, pair, side, text):
    raw = text.encode()
    if len(raw) <= HISTORY_BYTES:
        return text
    peer = "claude" if side == "codex" else "codex"
    digest = hashlib.sha256(raw).hexdigest()
    con.execute("INSERT OR IGNORE INTO handoffs VALUES (?,?,?,?,?)",
                (pair["codex"], pair["claude"], pair[peer], digest, text))
    if peer == "claude":
        # The Claude relay is a model. Give it a short reference so it cannot
        # accidentally paraphrase task constraints or reproduce lengthy logs.
        return f"Read the paired handoff excerpt now and act on it: {original_command(peer, digest)}"
    return handoff_excerpt(text, peer, digest)


def handoff_excerpt(text, peer, digest):
    raw = text.encode()
    head = raw[:1200].decode(errors="ignore")
    tail = raw[-1800:].decode(errors="ignore")
    command = original_command(peer, digest, len(head), len(text) - len(tail))
    return (f"[ccxp excerpt. The middle is omitted. If required instructions or evidence are missing, "
            f"retrieve them yourself before acting: {command}]\n{head}\n[…]\n{tail}")


def read_original(args):
    variable = "CODEX_THREAD_ID" if args.side == "codex" else "CLAUDE_CODE_SESSION_ID"
    row = database().execute(f"""SELECT h.body FROM handoffs h JOIN pairs p
        ON p.codex=h.codex AND p.claude=h.claude WHERE p.{args.side}=? AND h.recipient=p.{args.side}
        AND h.digest=? AND p.cwd=?""", (os.environ.get(variable), args.digest, str(Path.cwd().resolve()))).fetchone()
    if not row:
        raise ValueError("当前配对会话没有这份交接原文。")
    if args.start is None and args.end is None:
        print(handoff_excerpt(row["body"], args.side, args.digest))
        return
    if args.start is None or args.end is None or not 0 <= args.start < args.end <= len(row["body"]):
        raise ValueError("原文区间无效。")
    stop = min(args.start + 1800, args.end)
    print(row["body"][args.start:stop])
    if stop < args.end:
        print("Next: " + original_command(args.side, args.digest, stop, args.end), file=sys.stderr)


def reply_socket(pair):
    # AF_UNIX has a short pathname limit on macOS.
    name = hashlib.sha256((pair["codex"] + pair["claude"]).encode()).hexdigest()[:20]
    return ROOT / (name + ".sock")


def deliver_to_listener(pair, body):
    path = reply_socket(pair)
    if not path.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        try:
            sock.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
        # Once sent, a lost acknowledgment is ambiguous: never queue a second copy.
        sock.sendall(json.dumps(body, ensure_ascii=False).encode() + b"\n")
        sock.shutdown(socket.SHUT_WR)
        receipt = sock.recv(1)
        if receipt == b"0":
            return False  # The original turn ended before this message was consumed.
        if receipt != b"1":
            raise ValueError("本轮监听没有确认收件。请核对当前轮输出后再决定是否重发。")
    return True


def turn_ended(transcript):
    # Read only new, complete records; an interrupted tool process may outlive its Codex turn.
    while True:
        pos = transcript.tell()
        line = transcript.readline()
        if not line.endswith(b"\n"):
            transcript.seek(pos)
            return False
        if b'"event_msg"' not in line[:160]:
            continue
        record = json.loads(line)
        if record.get("type") == "event_msg" and record.get("payload", {}).get("type") in (
            "task_complete", "turn_aborted", "task_started"
        ):
            return True


def listen(request, timeout):
    cid = os.environ.get("CODEX_THREAD_ID")
    with database() as con:
        pair = con.execute("SELECT p.*,s.path FROM pairs p JOIN streams s ON s.sid=p.codex WHERE p.codex=?",
                           (cid,)).fetchone()
    if not pair or Path(pair["cwd"]).resolve() != Path.cwd().resolve():
        raise ValueError("请从已配对的 Codex 会话和工作区开始监听。")
    if timeout < 1:
        raise ValueError("监听时长必须为正数。")
    path = reply_socket(pair)
    # Reserve the name while reclaiming a socket left behind by an interrupted turn.
    with open(pair["path"], "rb") as transcript, path.with_suffix(".lock").open("a") as lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        transcript.seek(0, os.SEEK_END)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("此配对已有本轮监听，请继续读取原监听。") from None
        path.unlink(missing_ok=True)
        server.bind(str(path))
        try:
            server.listen()
            if request:
                print(send("codex", request), flush=True)
            print("本轮正在接收 Claude Code 回信。", flush=True)
            until = time.monotonic() + timeout
            while time.monotonic() < until and not turn_ended(transcript):
                server.settimeout(min(2, max(0.01, until - time.monotonic())))
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(10)
                    with conn.makefile("rb") as stream:
                        message = json.loads(stream.readline(65536))
                    if turn_ended(transcript):
                        conn.sendall(b"0")
                        break
                    print(message, flush=True)
                    conn.sendall(b"1")
        finally:
            path.unlink(missing_ok=True)


def send(side, text):
    """Deliver text into the paired session as a new turn, the way Claude Code sessions message each other."""
    variable = "CODEX_THREAD_ID" if side == "codex" else "CLAUDE_CODE_SESSION_ID"
    current = os.environ.get(variable)
    if not current:
        raise ValueError(f"当前客户端缺少 {variable}。请从该会话内调用。")
    if not text.strip():
        raise ValueError("消息不能为空。")
    with database() as con:
        con.execute("BEGIN IMMEDIATE")
        pair = con.execute(f"SELECT * FROM pairs WHERE {side}=?", (session_id(current),)).fetchone()
        if not pair:
            raise ValueError("当前会话没有配对，请先用 ccxp 绑定。")
        text = compact_handoff(con, pair, side, text)
    if side == "claude":
        body = "[Claude Code → Codex] " + text
        if deliver_to_listener(pair, body):
            return "当前 Codex 轮的监听已收到回信。"
        done = subprocess.run(["codex", "queue", "--thread", pair["codex"], "--resume", "--message", body],
                              cwd=pair["cwd"], capture_output=True, text=True, timeout=120)
        if done.returncode or "Queued message" not in done.stdout + done.stderr:
            raise ValueError("投递到 Codex 失败：" + (done.stderr or done.stdout).strip()[-400:])
        return "已投递到 Codex，会话空闲时自动开始处理。"
    # The live CLI listing includes auto-assigned names even before the first saved turn.
    listing = subprocess.run(["claude", "agents", "--json"], cwd=pair["cwd"], capture_output=True,
                             text=True, check=True, timeout=15)
    sessions = json.loads(listing.stdout)
    matches = [s for s in sessions if s.get("sessionId") == pair["claude"]]
    if len(matches) != 1 or Path(matches[0]["cwd"]).resolve() != Path(pair["cwd"]):
        raise ValueError(f"无法在配对工作区唯一定位运行中的 Claude Code 会话 {pair['claude']}。")
    name = matches[0].get("name")
    if not name or sum(s.get("name") == name for s in sessions) != 1:
        raise ValueError(f"Claude Code 当前没有为会话 {pair['claude']} 提供可唯一定位的消息名称。")
    body = "[Codex → Claude Code] " + text
    prompt = ("Relay this JSON object with a single SendMessage call. Copy the to and message values "
              "verbatim, without JSON quoting. Do not choose another recipient, retry, or execute the task.\n"
              + json.dumps({"to": name, "message": body}, ensure_ascii=False))
    # stream-json exposes the SendMessage tool result itself; the relay's closing prose is not a reliable signal.
    done = subprocess.run(["claude", "-p", "--model", "haiku", "--no-session-persistence",
                           "--output-format", "stream-json", "--verbose",
                           "--tools", "SendMessage,ToolSearch", "--allowedTools", "SendMessage,ToolSearch"],
                          input=prompt, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=180)
    sends = {}
    for line in done.stdout.splitlines():
        record = json.loads(line)
        for block in record.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "SendMessage":
                args = block.get("input", {})
                if args.get("to") == name:
                    sends[block["id"]] = args.get("message")
            call_id = block.get("tool_use_id")
            if block.get("type") != "tool_result" or call_id not in sends or block.get("is_error"):
                continue
            content = block.get("content", [])
            texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content]
            for value in texts:
                try:
                    receipt = json.loads(value)
                except ValueError:
                    continue
                if isinstance(receipt, dict) and receipt.get("success") is True:
                    if sends[call_id] != body:
                        return f"已投递到 Claude Code 会话 {pair['claude']}，但中继发送的文字与原文有差异。"
                    return f"已投递到 Claude Code 会话 {pair['claude']}（{name}）。"
    raise ValueError(f"未取得发往 Claude Code 会话 {pair['claude']} 的成功回执，请核对目标收件记录后再重试。")



def install(side):
    path = Path.home() / (".codex/hooks.json" if side == "codex" else ".claude/settings.json")
    old = path.read_bytes() if path.exists() else None
    cfg = json.loads(old) if old is not None else {}
    groups = cfg.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    command = shlex.join([sys.executable, str(SCRIPT), "hook", side])
    if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
        return
    handler = {"type": "command", "command": command, "timeout": 10}
    if side == "codex":
        handler["additionalContextLimit"] = 6000
    groups.append({"hooks": [handler]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp = Path(tmp.name)
        tmp.write((json.dumps(cfg, ensure_ascii=False, indent=2) + "\n").encode())
    try:
        if old is not None:
            temp.chmod(path.stat().st_mode & 0o777)
        if (path.read_bytes() if path.exists() else None) != old:
            raise RuntimeError(f"{path} changed during installation; retry.")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="cmd", required=True)
    cmds.add_parser("install")
    p = cmds.add_parser("create", help="Start a new peer in this workspace and pair it")
    p.add_argument("--from", dest="side", choices=["codex", "claude"], required=True)
    p = cmds.add_parser("bind")
    p.add_argument("--from", dest="side", choices=["codex", "claude"], required=True)
    p.add_argument("--peer", required=True)
    p.add_argument("--replace-peer-of", metavar="PREVIOUS_SESSION_ID")
    p = cmds.add_parser("send")
    p.add_argument("--from", dest="side", choices=["codex", "claude"], required=True)
    p.add_argument("--message", required=True)
    p = cmds.add_parser("listen", help="Receive paired replies in this Codex turn until timeout or interruption")
    p.add_argument("--request", help="Send a request to Claude after opening the listener")
    p.add_argument("--timeout", type=int, default=1800)
    p = cmds.add_parser("original")
    p.add_argument("--for", dest="side", choices=["codex", "claude"], required=True)
    p.add_argument("digest")
    p.add_argument("--start", type=int)
    p.add_argument("--end", type=int)
    cmds.add_parser("hook").add_argument("side", choices=["codex", "claude"])
    cmds.add_parser("open-claude").add_argument("codex_id")
    p = cmds.add_parser("history")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("sid", nargs="?")
    target.add_argument("--peer-of", choices=["codex", "claude"], help="Read the current session's peer")
    p.add_argument("--after", type=int, default=0, help="Exclusive sequence number")
    p.add_argument("--through", type=int, help="Inclusive sequence number")
    p.add_argument("--limit", type=int, help="Return the last N matches in chronological order")
    cmds.add_parser("unlink").add_argument("codex_id")
    args = parser.parse_args()
    if args.cmd == "install":
        for side in ("codex", "claude"):
            install(side)
    elif args.cmd == "hook":
        hook(args.side)
    elif args.cmd == "original":
        try:
            read_original(args)
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, f"{exc}\n")
    elif args.cmd == "listen":
        try:
            listen(args.request, args.timeout)
        except KeyboardInterrupt:
            return
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            parser.exit(1, f"{exc}\n")
    elif args.cmd == "send":
        try:
            print(send(args.side, args.message))
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            parser.exit(1, f"{exc}\n")
    elif args.cmd == "create":
        try:
            create_peer(args.side, Path.cwd().resolve())
        except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            parser.exit(1, f"{exc}\n")
    elif args.cmd == "bind":
        try:
            bind(args.side, args.peer, Path.cwd(), args.replace_peer_of)
        except (ValueError, OSError, sqlite3.Error) as exc:
            parser.exit(1, f"{exc}\n")
    else:
        con = database()
        if args.cmd == "history":
            if args.peer_of:
                side = args.peer_of
                variable = "CODEX_THREAD_ID" if side == "codex" else "CLAUDE_CODE_SESSION_ID"
                pair = con.execute(f"SELECT * FROM pairs WHERE {side}=?", (os.environ.get(variable),)).fetchone()
                if not pair or Path(pair["cwd"]).resolve() != Path.cwd().resolve():
                    parser.error("No paired session in this workspace")
                args.sid = pair["claude" if side == "codex" else "codex"]
            r = con.execute("SELECT * FROM pairs WHERE codex=? OR claude=?", (args.sid, args.sid)).fetchone()
            if not r:
                parser.error("No such paired session")
            with con:
                collect(con, "codex" if r["codex"] == args.sid else "claude", args.sid)
            if args.limit is not None and args.limit < 1:
                parser.error("--limit must be positive")
            # Keep the original unfiltered history format for existing callers.
            if args.limit is None and args.through is None and not args.after:
                rows = con.execute("SELECT role,body FROM messages WHERE sid=? ORDER BY seq", (args.sid,))
            else:
                rows = con.execute("""SELECT * FROM (SELECT seq,role,body FROM messages
                    WHERE sid=? AND seq>? AND (? IS NULL OR seq<=?)
                    ORDER BY seq DESC LIMIT ?) ORDER BY seq""",
                    (args.sid, args.after, args.through, args.through, args.limit or -1))
            for row in rows:
                print(json.dumps(dict(row), ensure_ascii=False))
            return
        row = con.execute("SELECT * FROM pairs WHERE codex=?", (args.codex_id,)).fetchone()
        if not row:
            parser.error("No such paired thread")
        if args.cmd == "unlink":
            with con:
                remove_pair(con, row)
            return
        os.chdir(row["cwd"])
        aid = row["claude"]
        started = any((Path.home()/".claude/projects").glob(f"*/{aid}.jsonl"))
        binary = shutil.which("claude") or str(Path.home()/".local/bin/claude")
        os.execv(binary, [binary, "--resume" if started else "--session-id", aid])


if __name__ == "__main__":
    os.umask(0o077)
    main()
