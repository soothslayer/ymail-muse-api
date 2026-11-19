import json
import os
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_discovery_and_safe_missing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, YAHOO_MUSE_CONFIG=directory + '/missing.json')
            params = StdioServerParameters(command=sys.executable, args=['-m', 'yahoo_muse.server'], env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.list_tools()
                    self.assertEqual({t.name for t in result.tools}, {'doctor', 'list_folders', 'search_messages', 'read_message', 'send_message', 'send_status'})
                    send = next(t for t in result.tools if t.name == 'send_message')
                    self.assertIn('idempotency_key', send.inputSchema['required'])
                    self.assertFalse(send.annotations.readOnlyHint)
                    result = await client.call_tool('search_messages', {'query': 'test'})
                    self.assertTrue(result.isError)
                    self.assertIn('setup', result.content[0].text)

    async def test_cli_error_is_json(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.run([sys.executable, '-m', 'yahoo_muse.cli', 'search', '-'], input='{}', text=True,
                                  capture_output=True, env=dict(os.environ, YAHOO_MUSE_CONFIG=directory + '/missing'))
            self.assertEqual(proc.returncode, 1)
            self.assertIn('setup', json.loads(proc.stdout)['error'])


if __name__ == '__main__':
    unittest.main()
