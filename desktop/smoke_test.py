"""Checks a BUILT desktop app actually works on this machine (run by the GitHub builds).

    python desktop/smoke_test.py "dist/Video Downloader.app/Contents/MacOS/Video Downloader"
    python desktop/smoke_test.py "dist/Video Downloader/Video Downloader.exe"

1. Runs the app with --self-test: ffmpeg, Deno, the gallery-dl helper, every page, a real H.264
   encode (Apple's encoder on a Mac), Pillow, and (if the site allows it) reading a YouTube link.
2. Starts the app as an MCP server (--mcp) and checks the AI tools answer.
Exit code 0 only if every required check passes.
"""
import os
import subprocess
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

APP = os.path.abspath(sys.argv[1])
ok = True

print("=== self-test ===", flush=True)
r = subprocess.run([APP, "--self-test"], capture_output=True, text=True, timeout=600)
print(r.stdout.strip() or "(no output)")
if r.stderr.strip():
    print("--- stderr (last lines) ---")
    print("\n".join(line for line in r.stderr.strip().splitlines()[-15:]))
if r.returncode != 0:
    ok = False
    print(f"self-test exit code {r.returncode}")


async def mcp_check():
    with anyio.fail_after(180):
        async with stdio_client(StdioServerParameters(command=APP, args=["--mcp"])) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                tools = [t.name for t in (await s.list_tools()).tools]
                print("tools:", ", ".join(tools))
                if len(tools) < 7:
                    raise RuntimeError("expected 7 tools")
                res = await s.call_tool("get_video_info", {"url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ"})
                text = res.content[0].text if res.content else ""
                # Cloud machines are often blocked by video sites: report, don't fail.
                print("get_video_info:", ("not available here: " if res.is_error else "")
                      + text[:120].replace("\n", " "))


print("\n=== MCP (AI assistants) ===", flush=True)
try:
    anyio.run(mcp_check)
    print("MCP: OK")
except Exception as e:  # noqa: BLE001
    ok = False
    print(f"MCP: FAILED {type(e).__name__}: {e}")

print("\nSMOKE TEST:", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)
