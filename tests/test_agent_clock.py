import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from vikram.agent import build_agent


class ClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_clock_refreshes_across_midnight_and_agent_reuse(self):
        zone = timezone(timedelta(hours=-7), "PDT")
        times = [
            datetime(2026, 10, 14, 23, 59, 59, tzinfo=zone),
            datetime(2026, 10, 15, 0, 0, 1, tzinfo=zone),
        ]
        for web in ("off", "auto"):
            with self.subTest(web=web):
                requests = []

                async def handler(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(
                        200,
                        json={
                            "id": "r",
                            "object": "chat.completion",
                            "created": 0,
                            "model": "local:1",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "The supplied time",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        },
                    )

                with patch("vikram.agent.datetime", create=True) as clock:
                    clock.now.return_value.astimezone.side_effect = times
                    async with httpx.AsyncClient(
                        transport=httpx.MockTransport(handler)
                    ) as client:
                        agent = build_agent("local:1", http_client=client, web=web)
                        result = await agent.run("What time is it?")
                        await agent.run(
                            "And now?", message_history=result.all_messages()
                        )
                instructions = [
                    next(m["content"] for m in r["messages"] if m["role"] == "system")
                    for r in requests
                ]
                self.assertIn("2026-10-14T23:59:59-07:00", instructions[0])
                self.assertIn("Wednesday", instructions[0])
                self.assertIn("PDT", instructions[0])
                self.assertIn("2026-10-15T06:59:59+00:00", instructions[0])
                self.assertIn("2026-10-15T00:00:01-07:00", instructions[1])
                self.assertNotIn("2026-10-14T23:59:59-07:00", instructions[1])
