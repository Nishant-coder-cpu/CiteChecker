"""Live end-to-end check: real registries, real ASI1, real Verifier agent.

Start `python verifier_agent.py` first (or don't, to exercise the fallback), then:
    python scripts/live_e2e.py demo/demo_references.txt [out.md]

Uses a throwaway client agent with no endpoint and the same handle_request /
make_remote_verify code path as agent.py.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402
from uagents import Agent, Context  # noqa: E402

from citecheck import net  # noqa: E402
from citecheck.messages import verifier_address  # noqa: E402
from citecheck.remote import make_remote_verify  # noqa: E402
from citecheck.service import handle_request  # noqa: E402

net.apply()
text = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2] if len(sys.argv) > 2 else "demo/last_report.md")
llm = AsyncOpenAI(base_url="https://api.asi1.ai/v1", api_key=os.environ["ASI_ONE_API_KEY"],
                  timeout=httpx.Timeout(float(os.getenv("ASI1_TIMEOUT", "20")), connect=3.0),
                  max_retries=4)
client = Agent(name="citecheck-e2e-client", seed="citecheck-e2e-client-throwaway-v1")


@client.on_event("startup")
async def run(ctx: Context):
    md = await handle_request(text, llm, make_remote_verify(ctx, verifier_address(os.environ["AGENT_SEED"])))
    out.write_text(md, encoding="utf-8")
    print(f"REPORT_WRITTEN {out}", flush=True)
    os._exit(0)


client.run()
