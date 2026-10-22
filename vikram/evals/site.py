"""An offline site for the browser evaluation case: canned pages, no network.

It answers the same engine protocol as the app's WebKit browser, so the case
measures how a model drives the shared browser tools (open, read, click) rather
than any web page. The price is only on a page whose address cannot be guessed,
so a correct answer means the model followed a link.
"""

HOME = "https://kettles.example/"
STEEL = "https://kettles.example/p/4c1e"
COPPER = "https://kettles.example/p/9b7a"
GLASS = "https://kettles.example/p/2d5f"
PAGES = {
    HOME: (
        "Kettle Corner",
        "Kettle Corner\nHandmade kettles, shipped weekly.\n"
        "Choose a kettle to see its price.",
        [("Steel Kettle", STEEL), ("Copper Kettle", COPPER), ("Glass Kettle", GLASS)],
    ),
    STEEL: (
        "Steel Kettle · Kettle Corner",
        "Steel Kettle\n1.7 litres, brushed steel.\nPrice: $36\nIn stock.",
        [("All kettles", HOME)],
    ),
    COPPER: (
        "Copper Kettle · Kettle Corner",
        "Copper Kettle\n1.5 litres, hammered copper.\nPrice: $48\nIn stock.",
        [("All kettles", HOME)],
    ),
    GLASS: (
        "Glass Kettle · Kettle Corner",
        "Glass Kettle\n1.2 litres, borosilicate glass.\nPrice: $41\nSold out.",
        [("All kettles", HOME)],
    ),
}


class ScriptedSite:
    """A browser engine over `PAGES`: every page is a list of links."""

    paused = False

    def __init__(self) -> None:
        self.history: list[str] = []

    async def resumed(self) -> None:
        return None

    def _page(self, url: str) -> dict:
        title, text, links = PAGES[url]
        return {
            "url": url,
            "title": title,
            "elements": [
                {"ref": number, "role": "link", "name": name}
                for number, (name, _) in enumerate(links, 1)
            ],
            "text": text,
        }

    async def act(self, call: dict, /) -> dict:
        action, current = call["action"], (self.history or [None])[-1]
        if action == "open":
            url = call["url"] if call["url"].endswith("/") else call["url"] + "/"
            url = url if url in PAGES else call["url"]
            if url not in PAGES:
                return {"ok": False, "error": "The page did not open."}
            self.history.append(url)
            return {"ok": True, "page": self._page(url)}
        if current is None:
            return {"ok": False, "error": "No page is open yet. Use browser_open."}
        if action == "click":
            links = PAGES[current][2]
            if not 1 <= call["element"] <= len(links):
                return {
                    "ok": False,
                    "error": f"Element {call['element']} is not on the current page.",
                    "page": self._page(current),
                }
            name, target = links[call["element"] - 1]
            self.history.append(target)
            return {"ok": True, "page": self._page(target), "target": f"link “{name}”"}
        if action == "back" and len(self.history) > 1:
            self.history.pop()
        if action == "type":
            return {
                "ok": False,
                "error": "There is no field to type into on this page.",
                "page": self._page(self.history[-1]),
            }
        return {"ok": True, "page": self._page(self.history[-1])}


def completed_browser(activity: list[dict]) -> list[str]:
    """`browser` once any browser action finished; never what it read."""
    return (
        ["browser"]
        if any(a["kind"] == "browser" and a["status"] == "complete" for a in activity)
        else []
    )
