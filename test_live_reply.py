"""Exercise the real relay processes in a disposable pair store and stub only Codex's queue."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import pair


class LiveReplyTest(unittest.TestCase):
    def test_delivery_lifetime_and_pair_isolation(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temp:
            base = Path(temp)
            store = base / "store"
            transcript = base / "turn.jsonl"
            transcript.touch()
            cid, aid, other_cid, other_aid = (str(uuid.uuid4()) for _ in range(4))
            with patch.object(pair, "ROOT", store), patch.object(pair, "DB", store / "pairs.sqlite3"):
                with pair.database() as con:
                    con.executemany("INSERT INTO pairs VALUES (?,?,?)",
                                    [(cid, aid, os.getcwd()), (other_cid, other_aid, os.getcwd())])
                    con.execute("INSERT INTO streams(sid,path) VALUES (?,?)", (cid, str(transcript)))
            queue = base / "codex"
            queue.write_text("#!/bin/sh\necho 'Queued message'\n")
            queue.chmod(0o700)
            env = dict(os.environ, SESSION_PAIR_DIR=str(store), CODEX_THREAD_ID=cid,
                       CLAUDE_CODE_SESSION_ID=aid, PATH=str(base) + os.pathsep + os.environ["PATH"])
            cli = [sys.executable, str(pair.SCRIPT)]

            def send(body, sender=aid):
                return subprocess.check_output(cli + ["send", "--from", "claude", "--message", body],
                                               env=dict(env, CLAUDE_CODE_SESSION_ID=sender), text=True, timeout=5)

            for attempt in range(2):
                with subprocess.Popen(cli + ["listen", "--timeout", "5"], env=env, stdout=subprocess.PIPE,
                                      text=True) as listener:
                    try:
                        self.assertIn("正在接收", listener.stdout.readline())
                        self.assertIn("空闲", send("unrelated", other_aid))
                        for body in ("first", "second"):
                            self.assertIn("已收到", send(body))
                            self.assertEqual(listener.stdout.readline().strip(), "[Claude Code → Codex] " + body)
                        if attempt == 0:
                            listener.kill()  # Stale socket must not prevent ordinary delivery or the next listener.
                        else:
                            transcript.write_text('{"type":"event_msg","payload":{"type":"task_complete"}}\n')
                            self.assertIn("空闲", send("late"))
                        listener.wait(timeout=4)
                    finally:
                        listener.kill()
                self.assertIn("空闲", send("idle"))


if __name__ == "__main__":
    unittest.main()
