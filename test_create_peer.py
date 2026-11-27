"""Check create and bind through the CLI with isolated client homes."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid

import pair


class CreatePeerTest(unittest.TestCase):
    def test_create_both_directions_and_keep_binding_on_start_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            cwd = Path(temp).resolve()
            cid, aid = str(uuid.uuid4()), str(uuid.uuid4())
            codex_dir = cwd / "codex-home/sessions/2026/11/27"
            claude_dir = cwd / "claude-home/projects/test"
            for path in (codex_dir, claude_dir):
                path.mkdir(parents=True)
            (codex_dir / f"rollout-{cid}.jsonl").write_text(json.dumps({
                "type": "session_meta", "payload": {"id": cid, "cwd": str(cwd)}}) + "\n")
            (claude_dir / f"{aid}.jsonl").write_text(json.dumps({"sessionId": aid, "cwd": str(cwd)}) + "\n")
            env = dict(os.environ, PATH=str(cwd) + os.pathsep + os.environ["PATH"],
                       CODEX_HOME=str(cwd / "codex-home"), CLAUDE_CONFIG_DIR=str(cwd / "claude-home"),
                       SESSION_PAIR_DIR=str(cwd / "store"), CODEX_THREAD_ID=cid, CLAUDE_CODE_SESSION_ID=aid)
            stub = """#!/usr/bin/env python3
import json, os, pathlib, sys, uuid
p = pathlib.Path.cwd()
assert 'CODEX_THREAD_ID' not in os.environ and 'CLAUDE_CODE_SESSION_ID' not in os.environ
if os.getenv('FAIL_START'):
    sys.exit(1)
sid = str(uuid.uuid4())
if sys.argv[1] == '--bg':
    name = sys.argv[sys.argv.index('--name')+1]
    (p/'claude-home/projects/test'/f'{sid}.jsonl').write_text(json.dumps(dict(sessionId=sid,cwd=str(p)))+'\\n')
    (p/'listing').write_text(json.dumps([dict(sessionId=sid,cwd=str(p),name=name)]))
elif sys.argv[1:] == ['agents', '--json']:
    print((p/'listing').read_text())
elif sys.argv[1:3] == ['exec','--json']:
    (p/'codex-home/sessions/2026/11/27'/f'rollout-{sid}.jsonl').write_text(
        json.dumps(dict(type='session_meta', payload=dict(id=sid,cwd=str(p))))+'\\n')
    print(json.dumps(dict(type='thread.started',thread_id=sid)))
else:
    sys.exit(2)
"""
            for client in ("claude", "codex"):
                (cwd / client).write_text(stub)
                (cwd / client).chmod(0o700)
            for side, current in (("codex", cid), ("claude", aid)):
                args = [sys.executable, str(pair.SCRIPT), "create", "--from", side]
                result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=True)
                binding = json.loads(result.stdout)
                self.assertEqual(binding[side], current)
                self.assertEqual(binding["workspace"], str(cwd))
                self.assertNotIn(binding["claude" if side == "codex" else "codex"], (cid, aid))
                failure = subprocess.run(args, cwd=cwd, env=dict(env, FAIL_START="1"), capture_output=True)
                self.assertNotEqual(failure.returncode, 0)
                with sqlite3.connect(cwd / "store/pairs.sqlite3") as db:
                    self.assertEqual(db.execute(f"SELECT codex,claude FROM pairs WHERE {side}=?", (current,)).fetchone(),
                                     (binding["codex"], binding["claude"]))
