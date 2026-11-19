"""Real Smol persistence across processes; no model calls or host mounts."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_toolkit import REAL_REASON, REAL_VM

from vikram import sandbox as vm
from vikram.sandbox_session import load_state, reset_sandbox, state_path

CHILD = """
import asyncio, json, sys
from pathlib import Path
from vikram import sandbox as vm
from vikram.sandbox_session import load_state
vm._markers = lambda: Path(sys.argv[2])
vm._swept = True
async def main():
    path = Path(sys.argv[1])
    sandbox = vm.VmSandbox(state_path=path)
    try:
        result = await sandbox.run("from pathlib import Path; print(Path('/workspace/persistence-proof.txt').read_text())")
        assert result.exit_code == 0, result.error
        print(json.dumps({'output': result.output.strip(), 'id': load_state(path).ref.id}))
    finally:
        await sandbox.close()
asyncio.run(main())
"""


@unittest.skipIf(REAL_REASON(), f"Local VM unavailable: {REAL_REASON()}")
class NativePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_stop_flushes_files_for_a_different_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = state_path(root, "e" * 32)
            markers = root / "markers"
            with (
                patch.object(vm, "_markers", return_value=markers),
                patch.object(vm, "_swept", True),
            ):
                sandbox = REAL_VM(state_path=path)
                try:
                    result = await sandbox.run(
                        "from pathlib import Path; Path('/workspace/persistence-proof.txt').write_text('survived')"
                    )
                    self.assertEqual(result.exit_code, 0, result.error)
                    await sandbox.close()
                    saved = load_state(path)
                    assert saved is not None
                    self.assertTrue(saved.saved)
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        CHILD,
                        str(path),
                        str(markers),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env={
                            "PATH": os.environ.get("PATH", ""),
                            "HOME": str(Path.home()),
                            "TMPDIR": tempfile.gettempdir(),
                        },
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            process.communicate(), 60
                        )
                    except BaseException:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                        raise
                    self.assertEqual(process.returncode, 0, stderr.decode())
                    payload = json.loads(stdout)
                    self.assertEqual(
                        payload, {"output": "survived", "id": saved.ref.id}
                    )
                    resumed = load_state(path)
                    assert resumed is not None
                    self.assertTrue(resumed.saved)
                finally:
                    await sandbox.close()
                    await reset_sandbox(path)
                self.assertFalse(path.exists())
                self.assertEqual(list(markers.glob("*.json")), [])
